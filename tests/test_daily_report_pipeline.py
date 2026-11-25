"""晨间采集后的日报生成与 token 分账测试。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import db
import pipeline


VALID_REPORT = {
    "headline": "晨间综合",
    "executive_summary": "摘要 [L1]",
    "sections": [
        {"id": "key_events", "bullets": ["事件 [L1]"]},
        {"id": "trends", "bullets": ["趋势 [L1]"]},
        {"id": "risks", "bullets": ["风险 [L1]"]},
        {"id": "watchlist", "bullets": ["观察 [L1]"]},
    ],
}


class FakeLLM:
    enabled = True

    def __init__(self):
        self.tokens = 0
        self.cached_tokens = 0
        self.reasoning_tokens = 0

    def chat(self, system, user, max_tokens=400):
        self.tokens += 100
        self.cached_tokens += 20
        self.reasoning_tokens += 30
        return json.dumps(VALID_REPORT, ensure_ascii=False)


class DailyReportPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("LLMINTEL_HOME")
        os.environ["LLMINTEL_HOME"] = self.tmp.name
        with patch.object(db, "seed_path", return_value=os.path.join(self.tmp.name, "missing.db")):
            db.init_db()
        self.run_id = db.start_run()
        db.log_probe(self.run_id, "test", "测试源", "ok", hit_count=1)
        conn = db.connect()
        try:
            cur = conn.execute("INSERT INTO snapshot (taken_at,max_item_id,run_id) VALUES (?,?,?)",
                               (db.now_iso(), 1, self.run_id))
            self.snapshot_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        db.add_tokens(50, "run")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("LLMINTEL_HOME", None)
        else:
            os.environ["LLMINTEL_HOME"] = self.old_home
        self.tmp.cleanup()

    def add_event(self):
        conn = db.connect()
        try:
            t = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = conn.execute(
                "INSERT INTO doc (guid,url,source_id,source_name,title,t_valid,t_txn,content,coverage_count) "
                "VALUES ('event','https://example.test/event','test','测试源','测试事件',?,?,'测试正文',1)", (t, t))
            conn.execute(
                "INSERT INTO item (doc_id,category,score,summary,entities,created_at) "
                "VALUES (?,'模型发布',0.8,'测试摘要','[\"智谱\"]',?)", (cur.lastrowid, t))
            conn.commit()
        finally:
            conn.close()

    def test_morning_report_tokens_are_accounted_separately(self):
        self.add_event()
        llm = FakeLLM()
        self.assertTrue(hasattr(pipeline, "_generate_daily_report"), "晨间日报编排缺失")
        result = pipeline._generate_daily_report(self.run_id, self.snapshot_id, llm)

        self.assertTrue(result["generated"])
        self.assertEqual(result["mode"], "llm")
        self.assertEqual(result["tokens"], 100)
        usage = db.usage_detail()
        self.assertEqual(usage["by_kind"]["run"]["tokens"], 50)
        self.assertEqual(usage["by_kind"]["daily_report"]["tokens"], 100)
        self.assertEqual(usage["cached"], 20)
        self.assertEqual(usage["reasoning"], 30)

    def test_morning_generation_refreshes_an_earlier_cached_report(self):
        import daily_report
        daily_report.build(save=True)
        self.add_event()
        llm = type("RuleLLM", (), {"enabled": False, "tokens": 0,
                                   "cached_tokens": 0, "reasoning_tokens": 0})()

        result = pipeline._generate_daily_report(self.run_id, self.snapshot_id, llm)

        self.assertTrue(result["generated"])
        self.assertEqual(daily_report.latest()["data_quality"]["independent_events"], 1)

    def test_report_failure_does_not_fail_collection_and_partial_cost_is_kept(self):
        self.add_event()
        llm = FakeLLM()

        def broken(**kwargs):
            llm.tokens += 40
            llm.cached_tokens += 5
            llm.reasoning_tokens += 10
            raise RuntimeError("report unavailable")

        self.assertTrue(hasattr(pipeline, "_generate_daily_report"), "晨间日报编排缺失")
        with patch("daily_report.ensure_report", side_effect=broken):
            result = pipeline._generate_daily_report(self.run_id, self.snapshot_id, llm)
        self.assertFalse(result["generated"])
        self.assertIn("report unavailable", result["error"])
        self.assertEqual(db.usage_detail()["by_kind"]["daily_report"]["tokens"], 40)


if __name__ == "__main__":
    unittest.main()
