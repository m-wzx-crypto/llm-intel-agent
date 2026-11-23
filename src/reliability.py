"""信源可靠度（启发式，不冒充权威）。

之前排序/加权用写死的 tier。这里用运行数据算一个可解释的分数：

  可靠度 = 0.55·可达率 + 0.30·首发率 + 0.15·转载覆盖
    - 可达率：最近 N 次 probe 中 ok/zero_hit 的占比（拿不到数的源要降权）
    - 首发率：同一新闻被多家转载时，该源成为「最早一条」的比例
    - 转载覆盖：该源稿件的平均 coverage_count（被多少家跟转）

样本不足（<10 条独立稿）会标 weak，分数仅供参考。
"""
import threading
import time

import db

_CACHE = {"at": 0.0, "data": None}
_LOCK = threading.Lock()


def _rate(a, b):
    return (a / b) if b else None


def build(days: int = 30) -> dict:
    now = time.monotonic()
    if _CACHE["data"] and now - _CACHE["at"] < 120:
        return _CACHE["data"]
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    conn = db.connect()
    try:
        probes = {}
        for r in conn.execute(
                "SELECT source_id,source_name,status,COUNT(*) n FROM probe_run "
                "WHERE run_id IN (SELECT id FROM run ORDER BY id DESC LIMIT 30) "
                "GROUP BY source_id,source_name,status"):
            sid = r["source_id"]
            d = probes.setdefault(sid, {"name": r["source_name"], "ok": 0, "bad": 0})
            d["name"] = d["name"] or r["source_name"]
            if r["status"] in ("ok", "zero_hit"):
                d["ok"] += r["n"]
            else:
                d["bad"] += r["n"]
        docs = {}
        for r in conn.execute(
                "SELECT source_id,source_name,COUNT(*) docs,"
                "SUM(CASE WHEN dupe_of IS NULL THEN 1 ELSE 0 END) canonical,"
                "AVG(COALESCE(coverage_count,1)) cov,"
                "SUM(CASE WHEN dupe_of IS NULL AND COALESCE(coverage_count,1)>1 "
                "THEN 1 ELSE 0 END) firsts "
                "FROM doc WHERE t_valid>=? GROUP BY source_id", (cutoff,)):
            docs[r["source_id"]] = dict(r)
    finally:
        conn.close()

    out = []
    for sid in set(probes) | set(docs):
        p = probes.get(sid) or {}
        d = docs.get(sid) or {}
        name = d.get("source_name") or p.get("name") or sid
        total = (p.get("ok", 0) + p.get("bad", 0))
        probe_rate = _rate(p.get("ok", 0), total)
        canonical = int(d.get("canonical") or 0)
        firsts = int(d.get("firsts") or 0)
        first_rate = _rate(firsts, canonical)
        cov = float(d.get("cov") or 1.0)
        score = (0.55 * (probe_rate if probe_rate is not None else 0.5)
                 + 0.30 * (first_rate if first_rate is not None else 0.0)
                 + 0.15 * min(1.0, cov / 2.0))
        weak = canonical < 10
        out.append({
            "source_id": sid, "name": name,
            "probe_rate": None if probe_rate is None else round(probe_rate, 3),
            "first_rate": None if first_rate is None else round(first_rate, 3),
            "coverage": round(cov, 2), "docs": canonical,
            "score": round(score, 3), "weak": weak,
        })
    out.sort(key=lambda x: -x["score"])
    data = {"status": "ok", "days": days, "sources": out,
            "note": "启发式分数：可达率/首发率/转载覆盖加权；样本<10 标 weak。",
            "at": db.now_iso()}
    _CACHE.update(at=now, data=data)
    return data


def weight(name: str, default: float = 0.5) -> float:
    """给对话/详情用的单源权重（0.2~1.0）。查不到就回默认值。"""
    try:
        for s in build()["sources"]:
            if s["name"] == name:
                return max(0.2, min(1.0, float(s["score"])))
    except Exception:
        pass
    return default
