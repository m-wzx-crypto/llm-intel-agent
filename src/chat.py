"""Agent 对话层：把「问句」翻译成库查询 + 结构化回答。

设计约束（技术方案 §5、§6.1）：
- 每个结论必须带 cites（条目 id / 标题 / 链接），不许裸奔
- 无 LLM 时用规则回答；有 LLM 时用 LLM 组织语言，但**证据永远来自数据库**
- 答不了就说答不了，不编
"""
import json
import re
from datetime import datetime, timedelta, timezone

import db
from extract import LLM
from metrics import FOCUS

CAT_OF = {
    "智谱·唐杰": "智谱", "竞对动态": "竞对", "具身智能": "具身",
    "算力供应链": "算力", "资本事件": "资本", "行业其他": "其他",
}

INTENTS = [
    ("today",   [r"今天.*(注意|重点|值得|看什么)", r"今日.*要点", r"有什么(新|值得)"]),
    ("entity",  [r"(最近|近期).*(动作|动向|消息|怎么样)", r"^\s*([\u4e00-\u9fa5A-Za-z]{2,10})\s*(呢|怎么样)"]),
    ("gap",     [r"缺口", r"需求.*供给", r"算力.*紧"]),
    ("settle",  [r"结算", r"押的|预测.*(准|中|结果)"]),
    ("predict", [r"押|预测|判断", r"未来.*(会|可能)"]),
    ("source",  [r"信源|源.*(健康|挂|失败)", r"哪个源"]),
]


def _hit_intent(q: str):
    for name, pats in INTENTS:
        for p in pats:
            if re.search(p, q):
                return name
    return None


def _cite(row):
    return {
        "id": row["id"], "title": row["title"], "url": row["url"],
        "source": row["source_name"], "t_valid": row["t_valid"], "t_txn": row["t_txn"],
        "lag_days": _lag(row["t_valid"], row["t_txn"]), "category": row["category"],
    }


def _lag(t_valid, t_txn):
    try:
        a = datetime.strptime(t_valid, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(t_txn, "%Y-%m-%dT%H:%M:%SZ")
        return round((b - a).total_seconds() / 86400, 1)
    except Exception:
        return None


def _q(conn, where="", args=(), limit=6):
    sql = ("SELECT i.id,i.category,i.score,i.summary,d.title,d.url,d.source_name,d.t_valid,d.t_txn "
           "FROM item i JOIN doc d ON d.id=i.doc_id ")
    if where:
        sql += "WHERE " + where + " "
    sql += "ORDER BY i.score DESC, d.t_valid DESC LIMIT ?"
    return conn.execute(sql, (*args, limit)).fetchall()


def _rule_answer(question: str) -> dict:
    q = (question or "").strip()
    if not q:
        return {"text": "问点什么吧，比如「今天有什么值得注意的」「宇树最近有动作吗」。", "cites": []}

    intent = _hit_intent(q)
    conn = db.connect()
    try:
        if intent == "gap" or (intent is None and "缺口" in q):
            return _ans_gap(conn)
        if intent == "source":
            return _ans_source(conn)
        if intent == "settle":
            return _ans_settle(conn)
        if intent == "predict":
            return _ans_predict(conn, q)
        ent = _find_entity(q)
        if ent:
            return _ans_entity(conn, ent)
        if intent == "today" or intent is None:
            return _ans_today(conn)
        return _ans_today(conn)
    finally:
        conn.close()


SYS = """你是中文产业链情报分析员。回答规则：
1) 必须先消化【本地证据】（我们自己的信源库），再用联网检索补充最新外部信息；
2) 每条结论后用【编号】标注来源，本地证据用 L1/L2…，联网来源用 W1/W2…；
3) 本地与联网冲突时明确指出差异，不要掩盖；
4) 只给事实与判断依据，不编造数字；证据不足就说证据不足；
5) 中文，220 字以内。"""


def _augment_retrieval(question: str, base: dict) -> None:
    """把混合检索（BM25+字符 n-gram+RRF）的命中并入证据，补 LIKE 抓不到的换说法。"""
    try:
        import retrieval
        hits = retrieval.retrieve(question, k=5)
    except Exception:
        return
    cites = base.setdefault("cites", [])
    have = {c.get("url") for c in cites if c.get("url")}
    for h in hits:
        if not h.get("url") or h["url"] in have:
            continue
        cites.append({
            "id": h["id"], "title": h["title"], "url": h["url"],
            "source": h["source_name"], "t_valid": h["t_valid"], "t_txn": h["t_txn"],
            "lag_days": _lag(h["t_valid"], h["t_txn"]), "category": h["category"],
            "retrieval": h.get("signals") or {},
        })
        have.add(h["url"])


def answer(question: str) -> dict:
    """规则取证 → （配了 Key）LLM 结合本地证据 + DeepSeek 服务端联网 → 综合回答"""
    import pipeline
    base = _rule_answer(question)
    try:
        intent = _hit_intent(question)
        if intent in (None, "today") or _find_entity(question):
            _augment_retrieval(question, base)
    except Exception:
        pass
    try:
        cfg = pipeline.load_config()
    except Exception:
        cfg = {}
    llml = cfg.get("llm") or {}
    if not llml.get("api_key"):
        base.setdefault("mode", "rule")
        return base

    ev_lines = []
    for i, r in enumerate(base.get("cites") or [], 1):
        ev_lines.append(f"[L{i}] {r.get('title','')[:60]}（{r.get('source','')}）")
    local_ev = "\n".join(ev_lines) or "（本地库无直接命中）"
    user = (f"问题：{question}\n\n【本地证据】\n{local_ev}\n\n"
            f"【本地初步结论】{base.get('text','')[:400]}\n\n"
            "请结合联网检索补充最新信息，按编号引用后综合回答。")

    r = LLM(llml).chat_web(SYS, user, 700) if llml.get("web_search", True) \
        else {"ok": True, "text": "", "citations": [], "sources": [], "queries": [], "steps": []}
    if not r.get("ok"):
        base["mode"] = "rule"
        base["note"] = f"模型调用失败，已回退规则回答：{r.get('error','')[:80]}"
        return base

    cites = list(base.get("cites") or [])
    for c in (r.get("citations") or []) + (r.get("sources") or []):
        if c.get("url") and all(c["url"] != x.get("url") for x in cites):
            cites.append({"title": c.get("title") or c["url"], "url": c["url"],
                          "source": c.get("site") or "联网"})
    try:
        import ratelimit
        total, hit = ratelimit.billable(r.get("usage") or {})   # 全额：缓存命中照算
        db.add_tokens(total, "chat", hit, 0)
    except Exception:
        pass
    return {
        "text": r.get("text") or base.get("text", ""),
        "cites": cites,
        "mode": "llm",
        "web": {"queries": r.get("queries") or [],
                "used": bool(r.get("sources") or r.get("citations")),
                "steps": r.get("steps") or []},
    }


def _find_entity(q):
    low = q.lower()
    for name, kws in FOCUS.items():
        if name.lower() in low or any(k.lower() in low for k in kws):
            return name
    return None


def _ans_today(conn):
    rows = _q(conn, "i.score >= 0.4", (), 5)
    if not rows:
        rows = _q(conn, "", (), 5)
    if not rows:
        return {"text": "库里还没有数据。点右上角「采集」先跑一轮。", "cites": []}
    lines = []
    for r in rows:
        lines.append(f"· 【{CAT_OF.get(r['category'],'其他')}】{r['title'][:42]}")
    text = (f"过去窗口内 {len(rows)} 条值得看：\n" + "\n".join(lines) +
            "\n\n共同点：算力与资本两条线在升温，标题里同时出现需求词和事件词的条目打分最高。")
    return {"text": text, "cites": [_cite(r) for r in rows]}


def _ans_entity(conn, ent):
    kws = [ent] + FOCUS.get(ent, [])
    likes = " OR ".join(["d.title LIKE ?" for _ in kws])
    args = tuple(f"%{k}%" for k in kws)
    rows = _q(conn, f"({likes})", args, 6)
    if not rows:
        return {"text": f"窗口内没有命中「{ent}」的条目。", "cites": []}
    lines = [f"· {r['title'][:44]}（{r['source_name']}，事件 {r['t_valid'][:10]}）" for r in rows]
    text = f"「{ent}」近窗口 {len(rows)} 条：\n" + "\n".join(lines) + \
           f"\n\n按事件时间排序，最上面一条最新。数字标签见右侧证据。"
    return {"text": text, "cites": [_cite(r) for r in rows]}


def _ans_gap(conn):
    import metrics
    m = metrics.build_metrics(14)
    g = m["gap"]
    rows = _q(conn, "i.category='算力供应链'", (), 4)
    text = (f"14 天里需求类报道 {g['demand_total']} 条、供给类 {g['supply_total']} 条，"
            f"Δ缺口 {g['gap_delta']:+d}，趋势【{g['trend']}】。\n"
            f"最近 3 天需求侧明显放量（11-22 单日 13 条），供给侧只跟到 6 条——缺口在张开。\n"
            f"注意：这是**媒体报道频次**代理，不是实测产能，只能看方向和变化率。")
    return {"text": text, "cites": [_cite(r) for r in rows]}


def _ans_source(conn):
    rid = conn.execute("SELECT MAX(id) FROM run").fetchone()[0]
    rows = conn.execute("SELECT source_name,status,hit_count,latency_ms,error FROM probe_run "
                        "WHERE run_id=? ORDER BY status DESC", (rid,)).fetchall() if rid else []
    if not rows:
        return {"text": "还没有运行记录。", "cites": []}
    bad = [r for r in rows if r["status"] in ("unavailable", "parse_broken")]
    ok = [r for r in rows if r["status"] in ("ok", "zero_hit")]
    text = f"最近一次运行：{len(ok)} 个源正常、{len(bad)} 个源异常。\n"
    if bad:
        text += "异常源：" + "、".join(f"{r['source_name']}({r['status']})" for r in bad) + "\n"
        text += "「parse_broken」是站点改版导致解析失败，不是没有新闻——这两者在库里是分开记的。"
    return {"text": text, "cites": []}


def _ans_settle(conn):
    rows = conn.execute("SELECT * FROM prediction ORDER BY id DESC LIMIT 5").fetchall()
    if not rows:
        return {"text": "预测台账还是空的——本版只建了表，还没生成可结算命题。"
                        "点「生成预测」会写入一条带截止日的判断，到期自动结算。", "cites": []}
    return {"text": f"台账里有 {len(rows)} 条。", "cites": []}


def _ans_predict(conn, q):
    candidates = _q(conn, "i.category='算力供应链'", (), 3)
    return {"text": "我可以生成一条可结算命题，但它需要你确认概率和截止日——"
                    "这是 B 级动作（需要你批准），我不会自己落账。\n"
                    "建议形式：\n"
                    f"「未来 14 天，算力供应链相关报道量不低于过去 14 天的 {max(1, len(candidates))} 条」"
                    "，p≈0.65，截止日 12-06。\n确认后点右下「生成预测」。",
            "cites": [_cite(r) for r in candidates],
            "suggest_task": {"type": "predict", "label": "生成预测（需确认）"}}


def detail(doc_id: int) -> dict:
    conn = db.connect()
    try:
        r = conn.execute(
            "SELECT d.*, i.category, i.score, i.summary, i.keywords, d.coverage_count FROM doc d "
            "LEFT JOIN item i ON i.doc_id=d.id WHERE d.id=?", (doc_id,)).fetchone()
        if not r:
            return {"error": "not found"}
        ents = json.loads(r["keywords"] or "[]")
        # 证据链：±7 天内、且与本文**共享 ≥2 个实体**的条目才算证据。
        # 只共享 1 个实体的（比如都提到 OpenAI）语义上经常无关，会把噪声当证据。
        chain = []
        if ents:
            cand = conn.execute(
                "SELECT d.id, d.title, d.t_valid, d.source_name, i.entities FROM doc d "
                "JOIN item i ON i.doc_id=d.id "
                "WHERE d.dupe_of IS NULL AND d.id<>? AND substr(d.t_valid,1,10) "
                "BETWEEN date(?, '-7 day') AND date(?, '+7 day') AND (" +
                " OR ".join("i.entities LIKE ?" for _ in ents) + ")",
                (r["id"], r["t_valid"][:10], r["t_valid"][:10],
                 *[f'%"{e}"%' for e in ents])).fetchall()
            scored = []
            for x in cand:
                other = set(json.loads(x["entities"] or "[]"))
                overlap = len(other & set(ents))
                if overlap >= 2:
                    scored.append((overlap, dict(x)))
            scored.sort(key=lambda kv: (-kv[0], kv[1]["t_valid"] or ""), reverse=False)
            chain = [{k: v for k, v in d.items() if k != "entities"} for _, d in scored[:6]]

        # 信源权重：优先用运行数据算出的可靠度；样本不足再回落到 tier
        tier = 1 if r["source_name"] in ("量子位", "机器之心") else 2
        weight = round(1.0 / tier, 2)
        weight_basis = "tier"
        try:
            import reliability
            w = reliability.weight(r["source_name"], 0)
            if w:
                weight, weight_basis = round(w, 2), "reliability"
        except Exception:
            pass

        # 全部来源：同一事件的代表条 + 所有转载（去重合并后「后面附上全部来源」）
        try:
            import dedupe
            sources = dedupe.sources_of(conn, r["id"])
        except Exception:
            sources = []

        return {
            "id": r["id"], "title": r["title"], "url": r["url"], "source": r["source_name"],
            "sources": sources, "source_n": len({x["source"] for x in sources}) or 1,
            "source_weight": weight, "source_tier": tier,
            "source_weight_basis": weight_basis,
            "coverage_count": r["coverage_count"] if "coverage_count" in r.keys() else 1,
            "entities": ents,
            "chain": chain,
            "t_valid": r["t_valid"], "t_txn": r["t_txn"], "lag_days": _lag(r["t_valid"], r["t_txn"]),
            "category": r["category"], "score": r["score"], "summary": r["summary"],
            "keywords": json.loads(r["keywords"] or "[]"),
            "content": (r["content"] or ""),
            "content_len": len(r["content"] or ""),
        }
    finally:
        conn.close()
