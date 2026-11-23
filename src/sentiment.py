"""态度复核：标题词典之外的 LLM 二次判定。

词典对反讽、否定句、条件句很容易判反（「不再被卡脖子」会被算成负面）。
这里在配置了 Key 时，把最近 N 条非行情标题交给模型逐条判定，并给出与词典的
一致率；没配 Key / 预算用完就只用词典，并明确标注 mode=dict。

成本受控：默认只复核 30 条，按条数收紧 max_tokens；结果不落库（避免把一次
复核固化成长期标签）。
"""
import json
import time
from collections import Counter

import db


def _rows(days: int, limit: int) -> list[dict]:
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT d.id,d.title,d.t_valid,d.source_name,i.summary,i.category "
            "FROM item i JOIN doc d ON d.id=i.doc_id "
            "WHERE d.dupe_of IS NULL AND substr(d.t_valid,1,10)>=? "
            "AND COALESCE(i.category,'')!='market' "
            "ORDER BY i.score DESC, d.t_valid DESC LIMIT ?", (cutoff, limit))]
    finally:
        conn.close()


def _parse_list(text: str) -> list:
    text = (text or "").strip()
    cands = []
    if "```" in text:
        for part in text.split("```"):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("[") or p.startswith("{"):
                cands.append(p)
    if "[" in text and "]" in text:
        cands.append(text[text.find("["):text.rfind("]") + 1])
    for c in cands:
        try:
            v = json.loads(c)
            if isinstance(v, list):
                return v
        except Exception:
            continue
    return []


def review(days: int = 30, limit: int = 30) -> dict:
    import metrics
    import pipeline
    from extract import LLM

    rows = _rows(days, limit)
    base = [metrics._polarity(r["title"] or "") for r in rows]
    dict_counts = dict(Counter(base))
    out = {
        "status": "ok", "mode": "dict", "days": days, "samples": len(rows),
        "dict": dict_counts,
        "note": "标题词典判定；配好 API Key 后可用模型复核反讽/否定句。",
        "items": [],
    }
    if not rows:
        out["status"] = "zero_hit"
        return out

    try:
        cfg = pipeline.apply_active_profile(pipeline.load_config())
        llm = LLM(cfg.get("llm") or {})
    except Exception:
        llm = None
    if not llm or not llm.enabled:
        out["note"] = "未配置 API Key，仅用词典。"
        return out

    lines = []
    for i, r in enumerate(rows, 1):
        text = f"{r.get('title','')}。{(r.get('summary') or '')[:80]}"
        lines.append(f"{i}. {text}")
    system = ("你是中文新闻态度判定器。只能输出 JSON 数组，不要解释。"
              "每条给 label（pos/neu/neg）和 why（20 字内）。"
              "注意反讽、否定、条件句；纯产品发布/融资/合作且无褒贬视为 neu。")
    user = ("逐条判断下列标题的态度：\n" + "\n".join(lines) +
            '\n\n输出格式：[{"i":1,"label":"pos","why":"..."}]')
    try:
        text = llm.chat(system, user, max_tokens=min(2400, 80 * len(rows) + 200))
        try:
            db.add_tokens(getattr(llm, "tokens", 0), "sentiment",
                          getattr(llm, "cached_tokens", 0),
                          getattr(llm, "reasoning_tokens", 0))
        except Exception:
            pass
    except Exception as e:
        out["note"] = f"模型复核失败，已回退词典：{str(e)[:80]}"
        return out

    got = {}
    for x in _parse_list(text):
        if not isinstance(x, dict):
            continue
        try:
            i = int(x.get("i") or x.get("id") or 0) - 1
        except Exception:
            continue
        lab = str(x.get("label") or "").lower()
        if 0 <= i < len(rows) and lab in ("pos", "neu", "neg"):
            got[i] = (lab, str(x.get("why") or "")[:40])
    if not got:
        out["note"] = "模型返回无法解析，已回退词典。"
        return out

    llm_labels = [got.get(i, (b, ""))[0] for i, b in enumerate(base)]
    agree = sum(1 for a, b in zip(base, llm_labels) if a == b)
    out.update({
        "mode": "llm",
        "llm": dict(Counter(llm_labels)),
        "agreement": round(agree / len(rows), 3),
        "reviewed": len(got),
        "note": "词典 vs 模型逐条对照；agreement 为一致率，不一致项值得人工看一眼。",
        "items": [{
            "id": r["id"], "title": r["title"], "t": (r["t_valid"] or "")[:10],
            "source": r["source_name"], "dict": base[i], "llm": llm_labels[i],
            "why": got.get(i, ("", ""))[1], "conflict": base[i] != llm_labels[i],
        } for i, r in enumerate(rows)],
    })
    return out
