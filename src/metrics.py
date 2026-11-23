"""指标层：为「三个视图」提供数据

三个问题 → 三组指标（技术方案 §6.2）：
  哪些缺口正在放大  → gap 序列（需求提及 − 供给提及）与其变化率
  哪些需求正在增长  → 实体提及量序列与其斜率
  钱/卡/资源从哪到哪 → 从文本规则抽取的 (from,to,channel,amount) 边

重要声明：本版没有产能/订单的真实数值，所有「量」均为**媒体报道频次**这一代理指标。
UI 必须显式标注「代理指标」，不得让客户误以为是实测产能。
抽不到的东西显示为空，不画假数据。
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import db
import flows as FLOWS

WINDOW = 14  # 天

# 需求侧 / 供给侧 词表（用于构造缺口代理）
DEMAND = ["算力", "GPU", "H100", "H200", "B200", "芯片", "缺卡", "采购", "订单", "涨价", "供不应求", "抢卡", "数据中心"]
SUPPLY = ["产能", "供货", "交付", "扩产", "库存", "降价", "冗余", "闲置", "过剩", "减产", "调配"]

# 关注主体（缺口/斜率图的对象）
FOCUS = {
    "智谱": ["智谱", "GLM", "ChatGLM"],
    "宇树": ["宇树", "Unitree"],
    "智元": ["智元", "Agibot"],
    "银河通用": ["银河通用", "Galbot"],
    "DeepSeek": ["DeepSeek", "深度求索"],
    "Kimi": ["Kimi", "月之暗面"],
    "Qwen": ["Qwen", "通义", "千问"],
    "豆包": ["豆包"],
    "英伟达": ["英伟达", "NVIDIA"],
    "昇腾": ["昇腾"],
}

def _days(window=WINDOW):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(window - 1, -1, -1)]


def _doc_rows(conn, window=WINDOW, q: str | None = None, full: bool = False):
    since = (datetime.now(timezone.utc) - timedelta(days=window + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cols = "d.id, d.title, d.t_valid, d.source_name, d.source_id, i.summary"
    if full:
        cols += ", d.content, d.url"
    sql = (f"SELECT {cols} FROM doc d "
           "LEFT JOIN item i ON i.doc_id=d.id "
           "WHERE d.t_valid>=? AND d.dupe_of IS NULL")
    args = [since]
    if q:
        sql += " AND (d.title LIKE ? OR d.content LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    return conn.execute(sql, args).fetchall()


def _half_slope(series):
    """后半段均值 - 前半段均值，衡量「正在放大」"""
    if len(series) < 4:
        return 0.0
    h = len(series) // 2
    a = sum(series[:h]) / max(1, h)
    b = sum(series[h:]) / max(1, len(series) - h)
    return round(b - a, 2)


def _today_new(conn, window: int) -> int:
    """今天新入库且落在本窗口内的条数（按 t_txn=我们得知的时间）。
    限定窗口：否则预置的历史文章会把「今日」顶到比窗口总数还大，界面自相矛盾。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=window + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return int(conn.execute(
        "SELECT count(*) FROM doc WHERE dupe_of IS NULL AND substr(t_txn,1,10)=? "
        "AND t_valid >= ?", (today, since)).fetchone()[0])


def build_metrics(window=WINDOW, q: str | None = None) -> dict:
    conn = db.connect()
    try:
        rows = _doc_rows(conn, window, q, full=True)
        days = _days(window)
        day_idx = {d: i for i, d in enumerate(days)}

        # 逐条落到「第几天」
        mentions = defaultdict(lambda: [0] * window)      # 实体 → 每日提及
        demand_series = [0] * window
        supply_series = [0] * window

        for r in rows:
            d = (r["t_valid"] or "")[:10]
            i = day_idx.get(d)
            text = f"{r['title']} {r['summary'] or ''}"
            if i is not None:
                low = text.lower()
                for name, kws in FOCUS.items():
                    if any(k.lower() in low for k in kws):
                        mentions[name][i] += 1
                if any(w in text for w in DEMAND):
                    demand_series[i] += 1
                if any(w in text for w in SUPPLY):
                    supply_series[i] += 1

        # ① 斜率图数据
        slopes = []
        for name, series in mentions.items():
            total = sum(series)
            if total == 0:
                continue
            slopes.append({
                "name": name,
                "series": series,
                "total": total,
                "slope": _half_slope(series),
                "last": series[-1],
                "weak": total < 5,   # 样本不足 5 条 → 前端置灰，不许当结论
            })
        slopes.sort(key=lambda x: (x["slope"], x["total"]), reverse=True)

        # ② 缺口条数据：需求 vs 供给
        gap_series = [d - s for d, s in zip(demand_series, supply_series)]
        # 口径说明：
        #   day_*        最后一天的需求/供给（对应图上最右一点）
        #   gap_now_7d   近 7 天累计缺口
        #   gap_prev_7d  前 7 天累计缺口
        #   total_*      整个窗口的总量（不要和 7 天混用）
        gap_now = sum(gap_series[-7:])
        gap_prev = sum(gap_series[:7]) if window >= 14 else 0
        gap_delta = gap_now - gap_prev
        gap = {
            "demand_series": demand_series,
            "supply_series": supply_series,
            "gap_series": gap_series,
            "gap_now": gap_now,
            "gap_delta": gap_delta,
            "demand_total": sum(demand_series),
            "supply_total": sum(supply_series),
            "gap_now_7d": gap_now,
            "gap_prev_7d": gap_prev,
            "demand_day": demand_series[-1] if demand_series else 0,
            "supply_day": supply_series[-1] if supply_series else 0,
            "trend": "放大" if gap_delta > 0 else ("收窄" if gap_delta < 0 else "持平"),
            "note": "以媒体报道频次代理需求/供给，非实测产能",
        }

        # ③ 资金/资源流向边（全文规则抽取，抽不到就空）
        ids = [r["id"] for r in rows]
        cache_key = (window, q or "", len(ids), max(ids) if ids else 0, min(ids) if ids else 0)
        edges, flow_meta = FLOWS.extract(rows, max_edges=32, cache_key=cache_key)

        # ④ 舆情双轨：固定看智谱（下轨=媒体报道，上轨=官网一手发布）
        #    原先取「提及量 top3」，智谱几乎永远进不去；而双轨本来就是给自家主体用的。
        media_series = [0] * window
        tangjie_series = [0] * window
        first_series = [0] * window
        for r in rows:
            i = day_idx.get((r["t_valid"] or "")[:10])
            if i is None:
                continue
            low = f"{r['title']} {r['summary'] or ''} {r['content'] or ''}".lower()
            if any(k in low for k in ("智谱", "glm", "chatglm", "zhipu")):
                media_series[i] += 1
            tlow = (r["title"] or "").lower()
            if "唐杰" in (r["title"] or "") or ("唐杰" in low and
                                                any(k in tlow for k in ("智谱", "glm", "清华"))):
                tangjie_series[i] += 1
            if (r["source_id"] or "") == "zhipu_news":
                first_series[i] += 1
        track = [{"subject": "智谱·唐杰", "volume": media_series,
                  "tangjie": tangjie_series, "utterance": first_series}]

        return {
            "today_new": _today_new(conn, window),
            "meta": {
                "window_days": window,
                "days": days,
                "doc_total": len(rows),
                "generated_at": db.now_iso(),
                "proxy_note": "全部量为媒体报道频次代理；一手产能/订单数据未接入",
            },
            "slope": slopes[:10],
            "gap": gap,
            "flows": edges,
            "flow_meta": flow_meta,
            "track": track,
            "evidence": {
                "gap":     {"n": sum(demand_series)+sum(supply_series), "kind": "proxy",
                            "unit": "篇报道", "min_n": 10,
                            "label": "报道频次代理，非实测产能/订单"},
                "slope":   {"n": sum(len(s["series"]) and s["total"] for s in slopes) if slopes else 0,
                            "kind": "proxy", "unit": "篇报道", "min_n": 5,
                            "label": "每实体报道数变化率"},
                "track":   {"n": sum(media_series) + sum(first_series),
                            "kind": "proxy", "unit": "篇", "min_n": 5,
                            "label": "媒体面=报道频次代理；一手=智谱官网新闻"},
                "flow":    {"n": flow_meta.get("total", len(edges)), "kind": "extracted",
                            "unit": "条关系", "min_n": 1,
                            "label": f"全文规则抽取 · 覆盖 {flow_meta.get('docs_scanned', 0)} 篇"},
                "forecast":{"n": len(_forecasts(conn)), "kind": "system", "unit": "条命题",
                            "min_n": 1, "label": "可结算，到期核对白名单信源"},
            },
            "forecasts": _forecasts(conn),
        }
    finally:
        conn.close()


def build_competitors(window=WINDOW, q: str | None = None) -> dict:
    """竞对面板：每个实体的提及量、变化率、最近条目"""
    conn = db.connect()
    try:
        rows = _doc_rows(conn, window, q)
        days = _days(window)
        day_idx = {d: i for i, d in enumerate(days)}
        agg = {}
        for r in rows:
            text = f"{r['title']} {r['summary'] or ''}"
            d = (r["t_valid"] or "")[:10]
            i = day_idx.get(d)
            import entities as ENT
            for raw in ENT.resolve(text)["entities"]:
                name = ENT.company_of(raw)
                if not name or name in ENT.SELF_COMPANIES:
                    continue
                a = agg.setdefault(name, {"name": name, "series": [0]*window,
                                          "items": [], "sources": set(), "aliases": set()})
                a["aliases"].add(raw)
                if i is not None:
                    a["series"][i] += 1
                a["items"].append({"id": r["id"], "title": r["title"],
                                   "t": r["t_valid"], "source": r["source_name"]})
                a["sources"].add(r["source_name"])
        out = []
        for a in agg.values():
            total = sum(a["series"])
            half = window // 2
            prev = sum(a["series"][:half]) / max(1, half)
            now = sum(a["series"][half:]) / max(1, window - half)
            a["items"].sort(key=lambda x: x["t"] or "", reverse=True)
            out.append({
                "name": a["name"], "n": total, "series": a["series"],
                "slope": round(now - prev, 2),
                "weak": total < 5,
                "sources": sorted(a["sources"]),
                "aliases": sorted(a["aliases"]),
                "recent": a["items"][:4],
            })
        out.sort(key=lambda x: (x["n"], x["slope"]), reverse=True)
        return {"window_days": window, "days": days, "competitors": out,
                "note": "竞对提及量 = 报道频次代理；样本 <5 条的下方标 * 不作结论"}
    finally:
        conn.close()


# 舆情态度：标题词典，只作趋势参考（中文反讽/省略会误判，UI 必须标注）
POS_WORDS = ("突破", "增长", "领先", "发布", "开源", "合作", "签约", "落地", "提速",
             "创新高", "融资", "认可", "首款", "第一", "登顶", "盈利", "增持", "看好",
             "超预期", "中标", "获批", "上线", "破纪录")
NEG_WORDS = ("下跌", "跌", "下滑", "亏损", "裁员", "质疑", "争议", "风险", "处罚",
             "罚款", "延期", "推迟", "失败", "不及预期", "泡沫", "重估", "减持",
             "破发", "收缩", "事故", "漏洞", "诉讼", "封禁", "下架", "解约", "承压")
_MARKET_RE = re.compile(r"港股|恒指|恒生|收盘|开盘|盘中|股价|A股|美股|指数|涨跌")


def _polarity(title: str) -> str:
    if _MARKET_RE.search(title) or re.search(r"[涨跌]\s*\d+(\.\d+)?%", title):
        return "market"
    p = sum(w in title for w in POS_WORDS)
    n = sum(w in title for w in NEG_WORDS)
    return "pos" if p > n else "neg" if n > p else "neu"


def build_zhipu(window=30, q: str | None = None) -> dict:
    """智谱·唐杰专题：媒体报道走势 + 唐杰提及 + 官网一手发布 + 标题态度（词典）"""
    conn = db.connect()
    try:
        rows = _doc_rows(conn, window, q, full=True)
        days = _days(window)
        idx = {d: i for i, d in enumerate(days)}
        media, tangjie, first = [0] * window, [0] * window, [0] * window
        pol = {"pos": 0, "neu": 0, "neg": 0, "market": 0}
        items, market_items = [], []
        for r in rows:
            title = r["title"] or ""
            summary = r["summary"] or ""
            low = f"{title} {summary} {r['content'] or ''}".lower()
            tlow = title.lower()
            official = (r["source_id"] or "") == "zhipu_news"
            is_zp = official or any(k in low for k in ("智谱", "glm", "chatglm", "zhipu"))
            # 唐杰：标题点名，或标题是智谱/清华而正文点名（避免富豪榜里被顺带列出）
            is_tj = ("唐杰" in title) or ("唐杰" in low and
                                          any(k in tlow for k in ("智谱", "glm", "清华")))
            if not (is_zp or is_tj):
                continue
            i = idx.get((r["t_valid"] or "")[:10])
            if i is not None:
                if is_zp:
                    media[i] += 1
                if is_tj:
                    tangjie[i] += 1
                if official:
                    first[i] += 1
            p = _polarity(title)
            pol[p] += 1
            x = {
                "id": r["id"], "title": title, "t": (r["t_valid"] or "")[:10],
                "source": r["source_name"] or "", "url": r["url"] or "",
                "summary": summary[:300], "kind": "first" if official else "media",
                "pol": p,
            }
            (market_items if p == "market" else items).append(x)
        items.sort(key=lambda x: x["t"] or "", reverse=True)
        market_items.sort(key=lambda x: x["t"] or "", reverse=True)
        src = defaultdict(int)
        for x in items + market_items:
            src[x["source"]] += 1
        # 一手清单不受窗口限制：官网新闻本身稀疏（季度/年度节点）
        fh = conn.execute(
            "SELECT d.id, d.title, d.t_valid, d.source_name, d.url FROM doc d "
            "JOIN item i ON i.doc_id=d.id WHERE d.source_id='zhipu_news' "
            "AND d.dupe_of IS NULL ORDER BY d.t_valid DESC LIMIT 12").fetchall()
        first_items = [{
            "id": x["id"], "title": x["title"] or "", "t": (x["t_valid"] or "")[:10],
            "source": x["source_name"] or "", "url": x["url"] or "",
            "summary": "", "kind": "first", "pol": _polarity(x["title"] or ""),
        } for x in fh]
        total_items = len(items) + len(market_items)
        return {
            "subject": "智谱·唐杰", "window_days": window, "days": days,
            "media": media, "tangjie": tangjie, "first_hand": first,
            "counts": {"media": sum(media), "tangjie": sum(tangjie),
                       "first_hand": len(first_items), "items": total_items,
                       "market": len(market_items), "sources": len(src)},
            "polarity": dict(pol, note="标题词典判定，只作趋势参考"),
            "items": items[:80] + market_items[:10],
            "first_items": first_items,
            "sources": [{"name": k, "n": v} for k, v in
                        sorted(src.items(), key=lambda kv: -kv[1])][:10],
            "evidence": {
                "media": {"kind": "proxy", "label": "标题/摘要命中 智谱/GLM/Zhipu 的篇数"},
                "tangjie": {"kind": "proxy", "label": "标题/摘要含 唐杰 的篇数"},
                "first": {"kind": "extracted", "label": "智谱官网新闻条目（一手）"},
                "polarity": {"kind": "proxy", "label": "标题词典，非情感模型"},
            },
        }
    finally:
        conn.close()


def build_timeline(window=WINDOW, q: str | None = None) -> dict:
    """事件时间轴：按天聚合，每天给最重要的几条（用 t_valid，不是入库时间）"""
    conn = db.connect()
    try:
        rows = _doc_rows(conn, window, q)
        days = _days(window)
        by_day: dict[str, list] = {d: [] for d in days}
        conn2 = conn
        for r in rows:
            d = (r["t_valid"] or "")[:10]
            if d not in by_day:
                continue
            row = conn2.execute(
                "SELECT i.score, i.category, i.entities FROM item i WHERE i.doc_id=?",
                (r["id"],)).fetchone()
            if not row:
                continue
            by_day[d].append({
                "id": r["id"], "title": r["title"], "source": r["source_name"],
                "t": r["t_valid"], "score": row["score"], "category": row["category"],
                "entities": __import__("json").loads(row["entities"] or "[]")[:3],
            })
        out = []
        for d in days:
            items = sorted(by_day[d], key=lambda x: -x["score"])
            out.append({"date": d, "n": len(items), "top": items[:3]})
        return {"window_days": window, "q": q, "days": out,
                "note": "按事件发生时间（t_valid）聚合，不是入库时间"}
    finally:
        conn.close()


def _forecasts(conn):
    """预测带：本版读台账；空则返回空数组（不编造）"""
    try:
        rows = conn.execute(
            "SELECT id,claim_text,deadline,p,state FROM prediction ORDER BY id DESC LIMIT 8").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
