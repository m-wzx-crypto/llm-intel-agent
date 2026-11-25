"""日报 HTTP 查询、生成与 Markdown 下载回归测试。"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import daily_report
import db
import locks
import server


class DailyReportApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("LLMINTEL_HOME")
        os.environ["LLMINTEL_HOME"] = self.tmp.name
        with patch.object(db, "seed_path", return_value=os.path.join(self.tmp.name, "missing.db")):
            db.init_db()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.report_date = datetime.now(daily_report.BUSINESS_TZ).date().isoformat()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
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
                "VALUES ('api-event','https://example.test/api','test','测试源','API 测试事件',?,?,'正文',1)",
                (t, t))
            conn.execute(
                "INSERT INTO item (doc_id,category,score,summary,entities,created_at) "
                "VALUES (?,'模型发布',0.8,'摘要','[\"智谱\"]',?)", (cur.lastrowid, t))
            conn.commit()
        finally:
            conn.close()

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_get_daily_report_returns_history(self):
        self.add_event()
        daily_report.build(report_date=self.report_date, save=True)
        status, headers, body = self.request("/api/daily-report?date=" + self.report_date)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(data["report"]["report_date"], self.report_date)
        self.assertEqual(data["reports"][0]["report_date"], self.report_date)

    def test_export_daily_report_returns_markdown_attachment(self):
        self.add_event()
        daily_report.build(report_date=self.report_date, save=True)
        status, headers, body = self.request("/api/daily-report/export?date=" + self.report_date)
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertIn(f'daily-report-{self.report_date}.md', headers["Content-Disposition"])
        self.assertIn("API 测试事件", body.decode())

    def test_post_daily_report_generates_rule_report_without_key(self):
        self.add_event()
        status, _, body = self.request("/api/daily-report", {"date": self.report_date, "force": True})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["generated"])
        self.assertEqual(data["mode"], "rule")
        self.assertIsNotNone(daily_report.get(self.report_date))

    def test_failed_generation_still_records_consumed_tokens(self):
        llm = type("FakeLLM", (), {"tokens": 55, "cached_tokens": 12,
                                   "reasoning_tokens": 3})()
        with patch.object(server, "_llm_for_cfg", return_value=llm), \
             patch.object(daily_report, "ensure_report", side_effect=RuntimeError("save failed")), \
             patch.object(db, "add_tokens") as add_tokens:
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                server._do_daily_report()

        add_tokens.assert_called_once_with(55, "daily_report", 12, 3)

    def test_manual_generation_is_rejected_while_collection_lock_is_held(self):
        handle = locks.acquire("data", blocking=False)
        self.assertIsNotNone(handle)
        try:
            with patch.object(daily_report, "ensure_report") as ensure:
                with self.assertRaisesRegex(RuntimeError, "采集"):
                    server._do_daily_report()
            ensure.assert_not_called()
        finally:
            locks.release(handle)

    def test_invalid_report_date_is_rejected(self):
        for value in ("not-a-date", "20261125", "2026-W47-1"):
            with self.subTest(value=value):
                status, _, body = self.request("/api/daily-report?date=" + value)
                self.assertEqual(status, 400)
                self.assertIn("日期", json.loads(body)["error"])

    def test_non_string_and_future_report_dates_are_rejected(self):
        future = (datetime.now(daily_report.BUSINESS_TZ).date()
                  + timedelta(days=1)).isoformat()
        for value in (20261125, future):
            with self.subTest(value=value):
                status, _, body = self.request("/api/daily-report", {"date": value})
                self.assertEqual(status, 400)
                self.assertTrue(json.loads(body)["ok"] is False)

    def test_generation_conflict_returns_http_409(self):
        handle = locks.acquire("data", blocking=False)
        self.assertIsNotNone(handle)
        try:
            status, _, body = self.request("/api/daily-report", {})
        finally:
            locks.release(handle)
        self.assertEqual(status, 409)
        self.assertIn("采集", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
