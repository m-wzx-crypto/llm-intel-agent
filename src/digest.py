"""简报生成：按技术方案 §6.1 的 digest.json 契约

铁律：
- 每条摘要 ≤ 120 字（理想 ≤40），长内容一律靠 URL 下钻
- 按板块配额裁剪，一屏放得下
- 所有数字来自库，不臆造
"""
import json
from datetime import datetime, timedelta, timezone

import db
from sources import CATEGORIES

CAT_KEY = {
    "智谱·唐杰": "zhipu",
    "竞对动态": "competitor",
    "具身智能": "embodied",
    "算力供应链": "compute",
    "资本事件": "capital",
    "行业其他": "other",
}


def build_digest(window_days: int = 3, per_panel: int = 8) -> dict:
    conn = db.connect()
    try:
        snap = conn.execute("SELECT * FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = conn.execute(
            """SELECT i.id, i.category, i.score, i.keywords, i.summary, i.created_at,
                      d.title, d.url, d.source_name, d.t_valid, d.fetched_ok
               FROM item i JOIN doc d ON d.id = i.doc_id
               WHERE d.t_valid >= ? OR d.t_txn >= ?
               ORDER BY i.score DESC, d.t_valid DESC""",
            (since, since)).fetchall()

        panels = {v: [] for v in CAT_KEY.values()}
        counts = {}
        for r in rows:
            key = CAT_KEY.get(r["category"], "other")
            counts[r["category"]] = counts.get(r["category"], 0) + 1
            if len(panels[key]) < per_panel:
                panels[key].append({
                    "id": r["id"],
                    "title": r["title"],
                    "summary": (r["summary"] or "")[:120],
                    "url": r["url"],
                    "source": r["source_name"],
                    "t": r["t_valid"],
                    "score": round(r["score"], 2),
                    "tags": json.loads(r["keywords"] or "[]")[:4],
                    "has_body": bool(r["fetched_ok"]),
                })

        # 源健康度（最近一次运行）
        last_run_id = conn.execute("SELECT MAX(id) FROM run").fetchone()[0]
        probes = []
        if last_run_id:
            probes = [dict(x) for x in conn.execute(
                "SELECT source_id, source_name, status, hit_count, latency_ms, error"
                " FROM probe_run WHERE run_id=? ORDER BY source_name", (last_run_id,)).fetchall()]

        run = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()

        return {
            "meta": {
                "snapshot_id": snap["id"] if snap else None,
                "taken_at": snap["taken_at"] if snap else None,
                "generated_at": db.now_iso(),
                "window_days": window_days,
                "data_dir": db.app_dir(),
                "items_in_window": len(rows),
                "last_run": dict(run) if run else None,
                "sources_total": len(probes),
                "sources_ok": sum(1 for p in probes if p["status"] in ("ok", "zero_hit")),
                "sources_bad": sum(1 for p in probes if p["status"] in ("unavailable", "parse_broken")),
            },
            "panels": panels,
            "counts": counts,
            "probes": probes,
            "forecasts": [],   # 预测台账本版仅建表，未产出
        }
    finally:
        conn.close()


def write_digest(path: str, window_days=3, per_panel=8) -> dict:
    d = build_digest(window_days, per_panel)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return d
