"""每日分析报告回归测试。"""
import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import db


VALID_LLM_REPORT = {
    "headline": "模型综合标题",
    "executive_summary": "模型综合摘要 [L1]",
    "sections": [
        {"id": "key_events", "title": "关键事件", "bullets": ["模型事件结论 [L1]"]},
        {"id": "trends", "title": "趋势与结构变化", "bullets": ["模型趋势结论 [L1]"]},
        {"id": "risks", "title": "风险与反证", "bullets": ["模型风险结论 [L1]"]},
        {"id": "watchlist", "title": "后续观察", "bullets": ["模型观察结论 [L1]"]},
    ],
}


class FakeLLM:
    def __init__(self, response, error=None):
        self.response = response
        self.error = error
        self.enabled = True
        self.tokens = 0
        self.calls = []

    def chat(self, system, user, max_tokens=400):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        self.tokens += 321
        if self.error:
            raise self.error
        return self.response


class DailyReportDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("LLMINTEL_HOME")
        os.environ["LLMINTEL_HOME"] = self.tmp.name
        with patch.object(db, "seed_path", return_value=os.path.join(self.tmp.name, "missing-seed.db")):
            db.init_db()

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("LLMINTEL_HOME", None)
        else:
            os.environ["LLMINTEL_HOME"] = self.old_home
        self.tmp.cleanup()

    def add_event(self, title, t_valid, *, dupe_of=None, source="测试源",
                  category="模型发布", score=0.8, coverage=1):
        conn = db.connect()
        try:
            cur = conn.execute(
                "INSERT INTO doc (guid,url,source_id,source_name,title,t_valid,t_txn,"
                "content,coverage_count,dupe_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title, f"https://example.test/{title}", "test", source, title,
                 t_valid, t_valid, f"{title}正文", coverage, dupe_of))
            doc_id = cur.lastrowid
            if dupe_of is None:
                conn.execute(
                    "INSERT INTO item (doc_id,category,score,summary,entities,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (doc_id, category, score, f"{title}摘要", '["智谱"]', t_valid))
            conn.commit()
            return doc_id
        finally:
            conn.close()

    def test_collect_excludes_reposts(self):
        try:
            import daily_report
        except ImportError:
            self.fail("daily_report 模块缺失")
        first = self.add_event("独立事件一", "2026-11-25T01:00:00Z")
        second = self.add_event("独立事件二", "2026-11-25T02:00:00Z", score=0.6)
        self.add_event("转载事件一", "2026-11-25T03:00:00Z", dupe_of=first, source="转载源")
        conn = db.connect()
        try:
            events = daily_report.collect(conn, "2026-11-24T00:00:00Z", "2026-11-25T23:59:59Z")
        finally:
            conn.close()
        self.assertEqual([e["id"] for e in events], [first, second])
        self.assertNotIn("转载事件一", [e["title"] for e in events])

    def test_build_rule_report_has_evidence_metrics_and_source_health(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first = self.add_event("智谱发布新模型", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        second = self.add_event("英伟达扩大供货", (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), category="资本事件", score=0.7)
        self.add_event("新模型转载", (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), dupe_of=first)
        run_id = db.start_run()
        db.log_probe(run_id, "good", "正常源", "ok", hit_count=2)
        db.log_probe(run_id, "bad", "故障源", "unavailable", error="timeout")

        import daily_report
        self.assertTrue(hasattr(daily_report, "build"), "daily_report.build 缺失")
        report = daily_report.build(now=now, run_id=run_id, save=False)

        self.assertEqual(report["mode"], "rule")
        self.assertEqual(report["data_quality"]["independent_events"], 2)
        self.assertEqual(report["data_quality"]["reposts_excluded"], 1)
        self.assertEqual([e["id"] for e in report["evidence"]], [first, second])
        self.assertEqual([s["id"] for s in report["sections"]],
                         ["key_events", "trends", "risks", "watchlist"])
        self.assertEqual(report["metrics"]["meta"]["window_days"], 14)
        self.assertIn("代理", report["metrics"]["meta"]["proxy_note"])
        self.assertEqual(report["data_quality"]["source_health"]["total"], 2)
        self.assertEqual(report["data_quality"]["source_health"]["unavailable"], 1)
        self.assertIn("[L1]", " ".join(report["sections"][0]["bullets"]))
        self.assertIn("故障源", json.dumps(report["data_quality"], ensure_ascii=False))

    def test_ensure_report_is_date_idempotent(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.add_event("首条事件", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        import daily_report
        self.assertTrue(hasattr(daily_report, "ensure_report"), "daily_report.ensure_report 缺失")
        report_date = now.astimezone(daily_report.BUSINESS_TZ).date().isoformat()
        first = daily_report.build(report_date=report_date, now=now, save=True)
        self.add_event("后到事件", (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), score=0.9)

        cached = daily_report.ensure_report(report_date=report_date, now=now)
        self.assertEqual(cached["generated_at"], first["generated_at"])
        self.assertEqual(cached["data_quality"]["independent_events"], 1)
        self.assertEqual(len(daily_report.listing()), 1)
        self.assertEqual(daily_report.get(report_date)["headline"], first["headline"])

        refreshed = daily_report.ensure_report(report_date=report_date, now=now, force=True)
        self.assertEqual(refreshed["data_quality"]["independent_events"], 2)
        self.assertEqual(len(daily_report.listing()), 1)

    def test_llm_synthesis_uses_local_evidence_and_preserves_citations(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.add_event("本地证据事件", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        llm = FakeLLM(json.dumps(VALID_LLM_REPORT, ensure_ascii=False))
        import daily_report
        self.assertTrue(hasattr(daily_report, "_narrate"), "daily_report._narrate 缺失")
        report = daily_report.build(now=now, llm=llm, save=False)

        self.assertEqual(report["mode"], "llm")
        self.assertEqual(report["tokens"], 321)
        self.assertEqual(report["headline"], "模型综合标题")
        self.assertEqual(report["sections"][0]["bullets"], ["模型事件结论 [L1]"])
        self.assertEqual(report["evidence"][0]["citation"], "L1")
        self.assertEqual(len(llm.calls), 1)
        self.assertIn('"citation": "L1"', llm.calls[0]["user"])
        self.assertIn("代理指标", llm.calls[0]["system"])
        self.assertIn("产能", llm.calls[0]["system"])

    def test_invalid_model_output_falls_back_without_losing_token_cost(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.add_event("本地证据事件", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        import daily_report
        self.assertTrue(hasattr(daily_report, "_narrate"), "daily_report._narrate 缺失")
        bad_citation = json.loads(json.dumps(VALID_LLM_REPORT))
        bad_citation["sections"][0]["bullets"] = ["引用不存在的证据 [L9]"]
        missing_section = json.loads(json.dumps(VALID_LLM_REPORT))
        missing_section["sections"].pop()
        cases = [
            ("invalid_json", FakeLLM("不是 JSON"), "JSON"),
            ("bad_citation", FakeLLM(json.dumps(bad_citation, ensure_ascii=False)), "引用"),
            ("missing_section", FakeLLM(json.dumps(missing_section, ensure_ascii=False)), "章节"),
            ("model_error", FakeLLM(None, RuntimeError("upstream unavailable")), "模型"),
        ]
        for name, llm, note_word in cases:
            with self.subTest(name=name):
                report = daily_report.build(now=now, llm=llm, save=False)
                self.assertEqual(report["mode"], "rule")
                self.assertEqual(report["tokens"], 321)
                self.assertIn(note_word, report["data_quality"]["llm_note"])
                self.assertEqual(report["evidence"][0]["citation"], "L1")

    def test_markdown_export_contains_sections_evidence_and_quality(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.add_event("导出测试事件", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        import daily_report
        self.assertTrue(hasattr(daily_report, "to_markdown"), "Markdown 导出缺失")
        report = daily_report.build(now=now, save=False)
        text = daily_report.to_markdown(report)

        self.assertIn("# " + report["headline"], text)
        self.assertIn("## 执行摘要", text)
        self.assertIn("## 关键事件", text)
        self.assertIn("[L1] 导出测试事件", text)
        self.assertIn("[原文](<https://example.test/导出测试事件>)", text)
        self.assertIn("媒体报道频次代理", text)
        self.assertIn("独立事件：1", text)

    def test_report_uses_business_date_and_rolling_window(self):
        import daily_report
        now = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
        self.add_event("跨 UTC 日期事件", "2026-11-24T17:30:00Z")
        report = daily_report.build(now=now, save=False)

        self.assertEqual(report["report_date"], "2026-11-25")
        self.assertEqual(report["window_start"], "2026-11-23T18:00:00Z")
        self.assertEqual(report["window_end"], "2026-11-24T18:00:00Z")
        self.assertEqual(report["metrics"]["meta"]["days"][-1], "2026-11-25")

    def test_historical_report_uses_requested_business_day(self):
        import daily_report
        now = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
        self.add_event("历史窗口事件", "2026-11-20T10:00:00Z")
        self.add_event("截止后事件", "2026-11-21T10:00:00Z")
        report = daily_report.build(report_date="2026-11-20", now=now, save=False)

        self.assertEqual(report["window_start"], "2026-11-19T15:59:59Z")
        self.assertEqual(report["window_end"], "2026-11-20T15:59:59Z")
        self.assertEqual(report["data_quality"]["independent_events"], 1)
        self.assertEqual(report["metrics"]["meta"]["doc_total"], 1)
        self.assertEqual(report["metrics"]["meta"]["days"][-1], "2026-11-20")

    def test_historical_report_uses_source_health_from_its_cutoff(self):
        import daily_report
        old_run = db.start_run()
        current_run = db.start_run()
        conn = db.connect()
        try:
            conn.execute("UPDATE run SET started_at=?,finished_at=? WHERE id=?",
                         ("2026-11-20T02:00:00Z", "2026-11-20T03:00:00Z", old_run))
            conn.commit()
        finally:
            conn.close()
        db.log_probe(old_run, "old", "历史故障源", "unavailable", error="timeout")
        db.log_probe(current_run, "current", "当前正常源", "ok", hit_count=1)
        now = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)

        report = daily_report.build(report_date="2026-11-20", now=now, save=False)

        self.assertEqual(report["run_id"], old_run)
        self.assertEqual(report["data_quality"]["source_health"]["unavailable"], 1)
        self.assertIn("历史故障源", json.dumps(report["data_quality"], ensure_ascii=False))

    def test_future_report_date_is_rejected(self):
        import daily_report
        now = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            daily_report.build(report_date="2026-11-26", now=now, save=False)

    def test_llm_report_requires_citations_in_summary_and_every_bullet(self):
        import daily_report
        evidence = [{"citation": "L1", "id": 1, "title": "证据"}]
        uncited_summary = json.loads(json.dumps(VALID_LLM_REPORT))
        uncited_summary["executive_summary"] = "没有引用的摘要"
        uncited_bullet = json.loads(json.dumps(VALID_LLM_REPORT))
        uncited_bullet["sections"][1]["bullets"] = ["没有引用的趋势"]
        web_citation = json.loads(json.dumps(VALID_LLM_REPORT))
        web_citation["sections"][1]["bullets"] = ["联网结论 [W1]"]
        for raw in (uncited_summary, uncited_bullet, web_citation):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    daily_report._validate_llm_report(json.dumps(raw, ensure_ascii=False), evidence)

    def test_truncation_preserves_valid_local_citations(self):
        import daily_report
        raw = json.loads(json.dumps(VALID_LLM_REPORT))
        raw["executive_summary"] = "摘" * 550 + " [L1]"
        raw["sections"][0]["bullets"] = ["事" * 550 + " [L1]"]

        report = daily_report._validate_llm_report(
            json.dumps(raw, ensure_ascii=False), [{"citation": "L1", "id": 1}])

        self.assertLessEqual(len(report["executive_summary"]), 500)
        self.assertLessEqual(len(report["sections"][0]["bullets"][0]), 320)
        self.assertTrue(report["executive_summary"].endswith("[L1]"))
        self.assertTrue(report["sections"][0]["bullets"][0].endswith("[L1]"))

    def test_daily_report_table_exists(self):
        conn = db.connect()
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("daily_report", tables)


if __name__ == "__main__":
    unittest.main()
