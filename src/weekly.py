"""按公司归类的周报：每周一份，回答「这一周谁在动、动了什么、谁在说」。

三条铁律（沿用技术方案 §6.1）：
- 所有数字来自库，不臆造；模型只负责把已有事实写成一句话
- 一句话 ≤ 90 字，长内容靠 item id 下钻
- 归属分强弱：标题点名 = 强归属，只在正文提到 = 弱归属。
  弱归属单独计数、不参与排序主键——否则「提到英伟达」的宏观文章会把英伟达顶到榜首

周的口径：ISO 周（周一 00:00 ~ 周日 23:59），与 launchd 周一 09:00 触发对齐。
"""
import json
import re
from datetime import date, datetime, timedelta, timezone

import db
import entities as ENT
from dedupe import headline as clean_title

MAX_COMPANIES = 24      # 最多列多少家公司
MAX_HIGHLIGHTS = 3      # 每家公司最多列几条代表作
MAX_NARRATE = 8         # 最多给几家公司写模型总结（控 token）
WEEKLY_BUDGET_TOKENS = 30000   # 头部 8 家各写一句，推理模型实测约 2.5k/家


# ── 周口径 ──
def week_bounds(anchor: date | None = None) -> tuple[str, str, str]:
    """返回 (start, end, label)：start/end 为 YYYY-MM-DD（含端点），label 形如 2026-W47"""
    d = anchor or date.today()
    start = d - timedelta(days=d.weekday())          # 周一
    end = start + timedelta(days=6)                   # 周日
    iso = start.isocalendar()
    return start.isoformat(), end.isoformat(), f"{iso[0]}-W{iso[1]:02d}"


def week_of(date_str: str) -> str:
    try:
        d = date.fromisoformat(date_str[:10])
    except Exception:
        return ""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _shift_week(start: str, weeks: int) -> tuple[str, str, str]:
    d = date.fromisoformat(start) + timedelta(weeks=weeks)
    return week_bounds(d)


# ── 取数 ──
def collect(conn, start: str, end: str) -> list[dict]:
    """该周的全部独立事件（已去重合并后的代表条）。"""
    rows = conn.execute(
        "SELECT d.id, d.title, d.source_name, d.t_valid, d.coverage_count, d.fetched_ok, "
        "       i.category, i.score, i.summary, i.entities "
        "FROM doc d JOIN item i ON i.doc_id = d.id "
        "WHERE d.dupe_of IS NULL AND substr(d.t_valid,1,10) BETWEEN ? AND ?",
        (start, end)).fetchall()
    out = []
    for r in rows:
        ents = json.loads(r["entities"] or "[]")
        out.append({
            "id": r["id"], "title": r["title"], "source": r["source_name"],
            "date": (r["t_valid"] or "")[:10], "coverage": r["coverage_count"] or 1,
            "category": r["category"], "score": r["score"] or 0,
            "summary": r["summary"] or "", "entities": ents,
        })
    return out


# 大盘/盘面综述类标题：对「某家公司本周发生了什么」没有信息量，排代表作时要压下去
GENERIC_TITLE = re.compile(
    r"恒生指数|沪指|深成指|道指|纳指|标普|收盘|收评|午评|早报|早餐|盘前|盘后|"
    r"涨停|跌停|主力资金|龙虎榜|融资余额|要闻汇总|重点关注|市场综述|盘面")


def _is_roundup(title: str) -> bool:
    """「A；B；C」多条快讯拼一条的汇总贴：信息量大但没有单一主体，代表作里要压后。"""
    return title.count("；") >= 2 or title.count("；") + title.count(";") >= 3


def _attr(item: dict) -> dict:
    """把一条事件摊成 {公司: 'strong'|'weak'}

    判定用清洗后的标题：快讯源的原标题是「【标题本体】正文开头」，
    直接拿整串去比会把正文里顺带提到的公司也算成「标题点名」。
    """
    title = clean_title(item["title"]).lower()
    out = {}
    for e in item["entities"]:
        co = ENT.company_of(e)
        if not co:
            continue
        strong = e.lower() in title or co.lower() in title
        if strong or co not in out:
            out[co] = "strong" if strong else "weak"
    return out


def group_by_company(items: list[dict], prev_items: list[dict] | None = None) -> list[dict]:
    """按公司聚合。返回按「本周热度」排序的公司列表。"""
    prev_cnt = {}
    for it in (prev_items or []):
        for co in _attr(it):
            prev_cnt[co] = prev_cnt.get(co, 0) + 1

    acc: dict[str, dict] = {}
    for it in items:
        for co, strength in _attr(it).items():
            g = acc.setdefault(co, {"company": co, "strong": 0, "weak": 0,
                                    "sources": set(), "categories": {},
                                    "items": [], "max_cov": 1})
            g[strength] += 1
            g["sources"].add(it["source"])
            if strength == "strong":     # 分类口径与 stories 一致，只算标题点名的事件
                g["categories"][it["category"]] = g["categories"].get(it["category"], 0) + 1
            g["max_cov"] = max(g["max_cov"], it["coverage"])
            if strength == "strong":
                g["items"].append(it)

    out = []
    for co, g in acc.items():
        if g["strong"] == 0:           # 只在正文被顺带提到的公司不进榜
            continue
        co_low = co.lower()
        picks = sorted(g["items"], key=lambda x: (
            0 if co_low in clean_title(x["title"]).lower() else 1,   # 标题里点名本公司的优先
            1 if (GENERIC_TITLE.search(clean_title(x["title"]))
                  or _is_roundup(clean_title(x["title"]))) else 0,  # 大盘/汇总贴靠后
            -x["coverage"], -x["score"], x["date"]))
        seen_titles = set()
        highlights = []
        for p in picks:
            key = p["title"][:18]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            highlights.append({"id": p["id"], "title": clean_title(p["title"]),
                               "raw_title": p["title"], "summary": p["summary"][:90],
                               "date": p["date"], "coverage": p["coverage"],
                               "source": p["source"], "category": p["category"]})
            if len(highlights) >= MAX_HIGHLIGHTS:
                break
        pc = prev_cnt.get(co, 0)
        delta = g["strong"] - pc
        out.append({
            "company": co,
            "stories": g["strong"],
            "mentions": g["strong"] + g["weak"],
            "sources": len(g["sources"]),
            "coverage": g["max_cov"],
            "categories": dict(sorted(g["categories"].items(), key=lambda kv: -kv[1])),
            "prev": pc,
            "delta": delta,
            "trend": "new" if pc == 0 else ("up" if delta > 0 else ("down" if delta < 0 else "flat")),
            "trend_txt": ("首次上榜" if pc == 0 else
                          (f"较上周 +{delta}" if delta > 0 else
                           (f"较上周 {delta}" if delta < 0 else "与上周持平"))),
            "highlights": highlights,
            "headline": "",
        })
    # 排序：事件数 → 媒体覆盖 → 提及数
    out.sort(key=lambda x: (-x["stories"], -x["coverage"], -x["mentions"]))
    return out


def rule_headline(g: dict) -> str:
    """无模型时的降级总结：把结构化的数字写成一句话，不编内容。"""
    cats = list(g["categories"].items())[:2]
    cat_txt = "、".join(f"{k}{v}条" for k, v in cats) if cats else "无分类"
    top = g["highlights"][0]["title"][:34] if g["highlights"] else "—"
    cover = f"，最多 {g['coverage']} 家同时报道" if g["coverage"] > 1 else ""
    return f"本周 {g['stories']} 起事件、{g['sources']} 个信源，以{cat_txt}为主{cover}。重点：{top}"


def narrate(groups: list[dict], llm=None) -> tuple[list[dict], int]:
    """给头部公司写一句话总结。有 Key 走模型，失败一律回落规则句（绝不编）。"""
    for g in groups:
        g["headline"] = rule_headline(g)
        g["headline_by"] = "rule"
    if not llm or not getattr(llm, "enabled", False):
        return groups, 0

    used = 0
    for g in groups[:MAX_NARRATE]:
        if used > WEEKLY_BUDGET_TOKENS:
            break
        facts = "；".join(f"{h['date']} {h['title']}（{h['coverage']}家报道）"
                         for h in g["highlights"])
        sys_p = ("你在写一份给投资/战略部门看的产业周报。基于给定事实，用中文写一句话总结，"
                 "要求：不超过90字；只陈述事实与主体动作；不评价、不预测、不加引号、不用'据悉'；"
                 "必须包含具体数字或事实（家数/金额/产品名）；没有的信息不许编。")
        usr = (f"公司：{g['company']}\n本周事件 {g['stories']} 起，"
               f"信源 {g['sources']} 个，分类 {g['categories']}\n事实：{facts}\n"
               f"（若信息不足，就照实概括，不要补充任何未给出的事实）")
        before = getattr(llm, "tokens", 0)
        try:
            s = llm.chat(sys_p, usr, max_tokens=220)
            used += max(0, getattr(llm, "tokens", 0) - before)
            s = (s or "").strip().strip('"“”').replace("\n", " ")
            s = re.sub(r"\s{2,}", " ", s)
            if 8 <= len(s) <= 600:
                # 模型爱超字数：截到 120 字以内最后一个句读，别把半句话挂在界面上
                if len(s) > 118:
                    cut = max(s.rfind("。", 0, 120), s.rfind("；", 0, 120), s.rfind("，", 0, 120))
                    s = s[:cut + 1] if cut >= 60 else s[:120] + "…"
                g["headline"] = s
                g["headline_by"] = "llm"
        except Exception:
            pass
    return groups, used


def rule_overview(payload_partial: dict) -> list[str]:
    """本周综述的规则版：四句话，每句都对应库里可核对的数字。"""
    t = payload_partial["totals"]
    groups = payload_partial["groups"]
    lines = []
    if not t["stories"]:
        return ["本周库里没有新事件——不是「什么都没发生」，是采集没跑或全部失败，先看信源健康度。"]
    lines.append(f"本周共 {t['stories']} 起事件、{t['sources']} 个信源，"
                 f"其中 {t['multi_source']} 起被 2 家以上同时报道（去重合并后计 1 起）。")
    if groups:
        top = groups[:3]
        lines.append("热度前三是 " + "、".join(
            f"{g['company']}（{g['stories']} 起，{g['trend_txt']}）" for g in top) + "。")
    if payload_partial["cats"]:
        tot = sum(payload_partial["cats"].values()) or 1
        seg = "、".join(f"{k} {v*100//tot}%" for k, v in list(payload_partial["cats"].items())[:3])
        lines.append(f"板块分布：{seg}。")
    news = [g["company"] for g in groups if g["trend"] == "new"][:6]
    if news:
        lines.append("本周新上榜：" + "、".join(news) + "（上周没有标题点名的事件）。")
    return lines


# ── 组装 ──
def build(start: str | None = None, llm=None, save: bool = True) -> dict:
    s, e, label = week_bounds(date.fromisoformat(start) if start else None)
    conn = db.connect()
    try:
        items = collect(conn, s, e)
        ps, pe, _ = _shift_week(s, -1)
        prev = collect(conn, ps, pe)
        groups = group_by_company(items, prev)
        groups, tokens = narrate(groups, llm)
        payload = {
            "week": label,
            "start": s,
            "end": e,
            "generated_at": db.now_iso(),
            "mode": "llm" if any(g.get("headline_by") == "llm" for g in groups) else "rule",
            "tokens": tokens,
            "totals": {
                "stories": len(items),
                "companies": len(groups),
                "multi_source": sum(1 for i in items if i["coverage"] > 1),
                "sources": len({i["source"] for i in items}),
                "prev_stories": len(prev),
                "prev_sources": len({i["source"] for i in prev}),
            },
            "cats": _cat_dist(items),
            "groups": groups[:MAX_COMPANIES],
        }
        payload["overview"] = rule_overview(payload)
        payload["overview_by"] = "rule"
        if save:
            _save(conn, payload)
        return payload
    finally:
        conn.close()


def _cat_dist(items: list[dict]) -> dict:
    d = {}
    for i in items:
        d[i["category"]] = d.get(i["category"], 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def _save(conn, payload: dict) -> None:
    conn.execute(
        "INSERT INTO weekly (week,start_date,end_date,generated_at,mode,tokens,payload) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(week) DO UPDATE SET start_date=excluded.start_date, "
        "end_date=excluded.end_date, generated_at=excluded.generated_at, "
        "mode=excluded.mode, tokens=excluded.tokens, payload=excluded.payload",
        (payload["week"], payload["start"], payload["end"], payload["generated_at"],
         payload["mode"], payload["tokens"],
         json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def latest() -> dict | None:
    conn = db.connect()
    try:
        r = conn.execute("SELECT payload FROM weekly ORDER BY week DESC LIMIT 1").fetchone()
        return json.loads(r["payload"]) if r else None
    finally:
        conn.close()


def get(week: str) -> dict | None:
    conn = db.connect()
    try:
        r = conn.execute("SELECT payload FROM weekly WHERE week=?", (week,)).fetchone()
        return json.loads(r["payload"]) if r else None
    finally:
        conn.close()


def latest_with_data() -> dict | None:
    """最近一份「有内容」的周报。默认展示它，而不是刚开周、还空着的本周。"""
    for row in listing(16):
        p = get(row["week"])
        if p and (p.get("totals") or {}).get("stories", 0) > 0:
            return p
    return latest()


def listing(limit: int = 12) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT week,start_date,end_date,generated_at,mode,tokens FROM weekly "
            "ORDER BY week DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def ensure_report(week: str | None = None, llm=None, force: bool = False) -> dict:
    """定时任务调用：指定周还没有报告就生成，已有就直接返回（幂等）。

    周一 09:00 跑的是「上一周」——本周才刚开始，没有完整数据。
    """
    if week:
        if not force:
            got = get(week)
            if got:
                return got
        d = date.fromisoformat(week_of_start(week))
        return build(start=d.isoformat(), llm=llm)
    ps, pe, _ = _shift_week(week_bounds()[0], -1)
    if not force:
        got = get(week_of(ps))
        if got:
            return got
    return build(start=ps, llm=llm)


def week_of_start(week: str) -> str:
    """2026-W47 → 该周周一的日期"""
    try:
        y, w = week.split("-W")
        return date.fromisocalendar(int(y), int(w), 1).isoformat()
    except Exception:
        return week_bounds()[0]
