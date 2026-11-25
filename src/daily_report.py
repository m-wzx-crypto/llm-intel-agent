"""每日自主分析报告：本地事实、趋势、风险与可追溯证据。"""
import json
import re
from collections import Counter
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone

import db
import metrics

MAX_EVIDENCE = 24
BUSINESS_TZ = timezone(timedelta(hours=8))
SECTION_IDS = ("key_events", "trends", "risks", "watchlist")
SECTION_TITLES = {
    "key_events": "关键事件",
    "trends": "趋势与结构变化",
    "risks": "风险与反证",
    "watchlist": "后续观察",
}


def _clip_with_citations(text: str, limit: int, refs: list[str]) -> str:
    if len(text) <= limit:
        return text
    suffix = "".join(f"[{ref}]" for ref in list(dict.fromkeys(refs))[:4])
    return text[:limit - len(suffix)].rstrip() + suffix


def collect(conn, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT d.id, d.title, d.url, d.source_id, d.source_name, d.t_valid, "
        "       d.coverage_count, i.category, i.score, i.summary, i.entities, i.evidence_kind "
        "FROM doc d JOIN item i ON i.doc_id=d.id "
        "WHERE d.dupe_of IS NULL AND d.t_valid>=? AND d.t_valid<=? "
        "ORDER BY i.score DESC, d.coverage_count DESC, d.t_valid DESC",
        (start, end)).fetchall()
    return [{
        "id": r["id"], "title": r["title"], "url": r["url"],
        "source_id": r["source_id"], "source": r["source_name"],
        "t_valid": r["t_valid"], "coverage": r["coverage_count"] or 1,
        "category": r["category"], "score": r["score"] or 0,
        "summary": r["summary"] or "", "entities": json.loads(r["entities"] or "[]"),
        "evidence_kind": r["evidence_kind"] or "proxy",
    } for r in rows]


def _source_health(conn, run_id: int | None, before: str | None = None) -> tuple[dict, int | None]:
    if run_id is None:
        if before:
            row = conn.execute(
                "SELECT id FROM run WHERE COALESCE(finished_at,started_at)<=? "
                "ORDER BY id DESC LIMIT 1", (before,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
        run_id = row["id"] if row else None
    if run_id is None:
        return {"run_id": None, "total": 0, "ok": 0, "zero_hit": 0,
                "unavailable": 0, "parse_broken": 0, "issues": []}, None
    rows = conn.execute(
        "SELECT source_id,source_name,status,hit_count,error FROM probe_run "
        "WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    counts = Counter(r["status"] for r in rows)
    issues = [{"source_id": r["source_id"], "name": r["source_name"],
               "status": r["status"], "hits": r["hit_count"],
               "error": r["error"] or ""}
              for r in rows if r["status"] not in ("ok", "zero_hit")]
    return {"run_id": run_id, "total": len(rows), "ok": counts["ok"],
            "zero_hit": counts["zero_hit"], "unavailable": counts["unavailable"],
            "parse_broken": counts["parse_broken"], "issues": issues[:12]}, run_id


def _rule_report(events: list[dict], metric_data: dict, health: dict,
                 reposts: int) -> dict:
    evidence = events[:MAX_EVIDENCE]
    cats = Counter(e["category"] for e in events)
    entities = Counter(e for event in events for e in event["entities"])
    multi = sum(1 for e in events if e["coverage"] > 1)
    top = evidence[0] if evidence else None
    top_cat = cats.most_common(1)[0][0] if cats else "暂无分类"

    if events:
        headline = f"{top_cat}事件最集中：24小时收录 {len(events)} 起独立事件"
        summary = (f"过去24小时共收录 {len(events)} 起独立事件，覆盖 "
                   f"{len({e['source'] for e in events})} 个信源；{multi} 起获得多源报道。"
                   f"当前最高优先级事件为「{top['title']}」[L1]。")
        key = [f"{e['title']}：{e['summary'] or '暂无摘要'} [L{i}]"
               for i, e in enumerate(evidence[:6], 1)]
    else:
        headline = "过去24小时暂无独立事件"
        summary = ("本地信源库在滚动24小时窗口内没有独立事件。"
                   "这不等于产业没有变化，需结合下方信源健康度判断是否采集异常。")
        key = ["窗口内没有可引用的独立事件，不生成推测性结论。"]

    trends = []
    if cats:
        total = len(events)
        trends.append("事件结构：" + "、".join(
            f"{name} {count} 起（{count * 100 // total}%）"
            for name, count in cats.most_common(3)) + "。")
    if events:
        trends.append(f"多源交叉验证：{multi} 起事件有 2 家以上报道，其余为单源信息。")
    strong = [s for s in metric_data.get("slope", []) if not s.get("weak")]
    if strong:
        s = strong[0]
        trends.append(f"14日报道频次中，{s['name']} 后半段较前半段变化 {s['slope']:+.2f} 篇/日；"
                      "这是媒体声量代理，不代表业务规模。")
    gap = metric_data.get("gap") or {}
    if (metric_data.get("evidence") or {}).get("gap", {}).get("n", 0) >= 10:
        trends.append(f"需求减供给的报道频次缺口近7日为 {gap.get('gap_now_7d', 0):+d}，"
                      f"较前7日变化 {gap.get('gap_delta', 0):+d}；非实测供需。")
    if not trends:
        trends.append("现有样本不足以识别稳定趋势。")

    risks = []
    bad = health["unavailable"] + health["parse_broken"]
    if bad:
        names = "、".join(i["name"] for i in health["issues"][:3])
        risks.append(f"采集质量：{bad} 个信源异常（{names}），事件总量可能偏低。")
    elif health["total"] == 0:
        risks.append("采集质量：没有可用的探针记录，无法判断信源覆盖是否完整。")
    risks.append("所有量与趋势均为媒体报道频次代理，不等同于实测产能、订单、收入或市场份额。")
    risks.append(f"单日样本仅 {len(events)} 起独立事件，结论应随后续信源交叉验证修正。")

    watch = []
    if entities:
        watch.append("优先跟踪 " + "、".join(
            f"{name}（{count} 起）" for name, count in entities.most_common(4))
                     + " 的后续发布、供货与资本动作。")
    if multi:
        watch.append(f"复核 {multi} 起多源事件是否出现官方确认、数量口径或时间表变化。")
    watch.append("次日对照新增信源与事件归并结果，避免把转载热度当作新增事实。")

    return {
        "headline": headline,
        "executive_summary": summary,
        "sections": [{"id": sid, "title": SECTION_TITLES[sid], "bullets": bullets}
                     for sid, bullets in zip(SECTION_IDS, (key, trends, risks, watch))],
        "evidence": [dict(e, citation=f"L{i}") for i, e in enumerate(evidence, 1)],
        "metrics": metric_data,
        "data_quality": {
            "window": "rolling_24h",
            "independent_events": len(events),
            "reposts_excluded": reposts,
            "evidence_limit": MAX_EVIDENCE,
            "source_health": health,
            "proxy_note": "全部量为媒体报道频次代理；一手产能/订单数据未接入",
        },
    }


def _validate_llm_report(raw: str, evidence: list[dict]) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except Exception as e:
        raise ValueError("JSON 格式无效") from e
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    headline = data.get("headline")
    summary = data.get("executive_summary")
    if not isinstance(headline, str) or not headline.strip():
        raise ValueError("标题缺失")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("执行摘要缺失")
    sections = data.get("sections")
    if not isinstance(sections, list):
        raise ValueError("章节格式无效")
    by_id = {}
    for section in sections:
        if not isinstance(section, dict) or section.get("id") not in SECTION_IDS:
            raise ValueError("章节 ID 无效")
        sid = section["id"]
        if sid in by_id:
            raise ValueError("章节重复")
        by_id[sid] = section
    if set(by_id) != set(SECTION_IDS):
        raise ValueError("章节不完整")

    valid = {f"L{i}" for i in range(1, len(evidence) + 1)}
    summary_refs = re.findall(r"\[([LW]\d+)\]", summary)
    if not summary_refs or any(ref not in valid for ref in summary_refs):
        raise ValueError("执行摘要缺少有效证据引用")
    all_text = [headline, summary]
    clean_sections = []
    for sid in SECTION_IDS:
        bullets = by_id[sid].get("bullets")
        if not isinstance(bullets, list) or not bullets:
            raise ValueError("章节缺少要点")
        clean = []
        for bullet in bullets[:4]:
            if not isinstance(bullet, str) or not bullet.strip():
                raise ValueError("要点格式无效")
            bullet = re.sub(r"【([LW]\d+)】", r"[\1]", bullet.strip())
            refs = re.findall(r"\[([LW]\d+)\]", bullet)
            if not refs or any(ref not in valid for ref in refs):
                raise ValueError("要点缺少有效证据引用")
            clean.append(_clip_with_citations(bullet, 320, refs))
            all_text.append(clean[-1])
        clean_sections.append({"id": sid, "title": SECTION_TITLES[sid], "bullets": clean})
    if not re.search(r"\[L\d+\]", "\n".join(all_text)):
        raise ValueError("报告缺少证据引用")
    summary = _clip_with_citations(summary.strip(), 500, summary_refs)
    return {"headline": headline.strip()[:100], "executive_summary": summary,
            "sections": clean_sections}


def _narrate(payload: dict, llm=None) -> tuple[dict, int, str]:
    if not llm or not getattr(llm, "enabled", False):
        return payload, 0, ""
    if not payload["evidence"]:
        return payload, 0, "没有独立事件，保留规则报告。"
    system = (
        "你是中文产业链情报分析师，只根据用户提供的本地证据写每日自主分析报告。"
        "输出严格 JSON，不要 Markdown。字段为 headline、executive_summary、sections；"
        "sections 必须依次包含 key_events、trends、risks、watchlist，"
        "每项含 id、title、bullets。执行摘要与每个要点都必须用 [L1]、[L2] 标注本地证据编号；"
        "不得引用不存在的编号或 [W] 联网编号，不得补充证据外事实。明确媒体报道频次是代理指标，"
        "不等同于实测产能、订单、收入或市场份额。中文，先结论后依据，不编造数字。")
    metric_data = payload["metrics"]
    pack = {
        "report_date": payload["report_date"],
        "window_start": payload["window_start"],
        "window_end": payload["window_end"],
        "totals": {"independent_events": payload["data_quality"]["independent_events"],
                   "reposts_excluded": payload["data_quality"]["reposts_excluded"]},
        "evidence": payload["evidence"],
        "source_health": payload["data_quality"]["source_health"],
        "metrics": {"meta": metric_data.get("meta", {}),
                    "slope": metric_data.get("slope", [])[:5],
                    "gap": metric_data.get("gap", {}),
                    "evidence": metric_data.get("evidence", {})},
    }
    before = getattr(llm, "tokens", 0)
    try:
        raw = llm.chat(system, json.dumps(pack, ensure_ascii=False), max_tokens=2400)
        narrate = _validate_llm_report(raw, payload["evidence"])
    except Exception as e:
        return payload, max(0, getattr(llm, "tokens", 0) - before), (
            f"模型综合失败，已回退规则报告：{str(e)[:120]}")
    payload.update(narrate)
    payload["mode"] = "llm"
    return payload, max(0, getattr(llm, "tokens", 0) - before), "模型仅基于本地证据完成综合。"


def build(report_date: str | None = None, *, now: datetime | None = None,
          run_id: int | None = None, snapshot_id: int | None = None,
          llm=None, save: bool = True) -> dict:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    business_now = current.astimezone(BUSINESS_TZ)
    report_date = report_date or business_now.date().isoformat()
    try:
        requested_date = date_cls.fromisoformat(report_date)
    except (TypeError, ValueError) as e:
        raise ValueError("报告日期格式应为 YYYY-MM-DD") from e
    if requested_date.isoformat() != report_date:
        raise ValueError("报告日期格式应为 YYYY-MM-DD")
    if requested_date > business_now.date():
        raise ValueError("不能生成未来日期的报告")
    if requested_date == business_now.date():
        end_dt = current
    else:
        end_dt = datetime.combine(requested_date, time.max, BUSINESS_TZ).replace(microsecond=0)
    end_utc = end_dt.astimezone(timezone.utc)
    start = (end_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.connect()
    try:
        events = collect(conn, start, end)
        reposts = conn.execute(
            "SELECT count(*) FROM doc WHERE dupe_of IS NOT NULL AND t_valid>=? AND t_valid<=?",
            (start, end)).fetchone()[0]
        health, resolved_run = _source_health(conn, run_id, before=end)
        metric_data = metrics.build_metrics(window=14, as_of=end_dt.astimezone(BUSINESS_TZ))
        payload = {
            "report_date": report_date,
            "generated_at": db.now_iso(),
            "mode": "rule",
            "tokens": 0,
            "window_start": start,
            "window_end": end,
            "run_id": resolved_run,
            "snapshot_id": snapshot_id,
        }
        payload.update(_rule_report(events, metric_data, health, reposts))
        payload, tokens, llm_note = _narrate(payload, llm)
        payload["tokens"] = tokens
        payload["data_quality"]["llm_note"] = llm_note
        if save:
            _save(payload)
        return payload
    finally:
        conn.close()


def _save(payload: dict) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO daily_report (report_date,snapshot_id,run_id,generated_at,mode,tokens,payload) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(report_date) DO UPDATE SET "
            "snapshot_id=excluded.snapshot_id, run_id=excluded.run_id, "
            "generated_at=excluded.generated_at, mode=excluded.mode, "
            "tokens=excluded.tokens, payload=excluded.payload",
            (payload["report_date"], payload.get("snapshot_id"), payload.get("run_id"),
             payload["generated_at"], payload["mode"], payload["tokens"],
             json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def get(report_date: str) -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT payload FROM daily_report WHERE report_date=?",
                           (report_date,)).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()


def latest() -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT payload FROM daily_report ORDER BY report_date DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()


def listing(limit: int = 14) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT report_date,generated_at,mode,tokens FROM daily_report "
            "ORDER BY report_date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def to_markdown(report: dict) -> str:
    def clean(value):
        return str(value or "").replace("\r", " ").replace("\n", " ").strip()

    quality = report.get("data_quality") or {}
    health = quality.get("source_health") or {}
    lines = [f"# {clean(report.get('headline'))}",
             f"> {report.get('report_date', '')} · {'模型综合' if report.get('mode') == 'llm' else '规则报告'}",
             "", "## 执行摘要", clean(report.get("executive_summary")), ""]
    for section in report.get("sections") or []:
        lines.extend([f"## {clean(section.get('title'))}"])
        lines.extend(f"- {clean(bullet)}" for bullet in section.get("bullets") or [])
        lines.append("")
    lines.append("## 证据")
    for item in report.get("evidence") or []:
        url = clean(item.get("url"))
        link = f" [原文](<{url}>)" if url else ""
        lines.append(f"- [{clean(item.get('citation'))}] {clean(item.get('title'))}"
                     f" — {clean(item.get('source'))} · {clean(item.get('t_valid'))[:10]}{link}")
    lines.extend(["", "## 数据质量",
                  f"- 独立事件：{quality.get('independent_events', 0)}",
                  f"- 已排除转载：{quality.get('reposts_excluded', 0)}",
                  f"- 信源探针：{health.get('total', 0)}，异常 "
                  f"{health.get('unavailable', 0) + health.get('parse_broken', 0)}",
                  f"- {clean(quality.get('proxy_note') or '全部量为媒体报道频次代理')}"])
    note = clean(quality.get("llm_note"))
    if note:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def ensure_report(report_date: str | None = None, *, force: bool = False,
                  now: datetime | None = None, run_id: int | None = None,
                  snapshot_id: int | None = None, llm=None) -> dict:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    report_date = report_date or current.astimezone(BUSINESS_TZ).date().isoformat()
    if not force:
        cached = get(report_date)
        if cached:
            return cached
    return build(report_date=report_date, now=now, run_id=run_id,
                 snapshot_id=snapshot_id, llm=llm, save=True)
