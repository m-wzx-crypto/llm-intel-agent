"""Agent 对话模式回归测试。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import chat
import pipeline


class ChatModeTests(unittest.TestCase):
    def test_non_web_mode_calls_normal_chat_completion(self):
        cfg = {"llm": {"api_key": "test-key", "model": "test-model", "web_search": False}}
        with patch.object(chat, "_rule_answer", return_value={"text": "规则回答", "cites": []}), \
             patch.object(chat, "_augment_retrieval"), \
             patch.object(pipeline, "load_config", return_value=cfg), \
             patch.object(chat.LLM, "chat", return_value="模型回答") as complete:
            result = chat.answer("测试问题")

        self.assertEqual(result["text"], "模型回答")
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["max_tokens"], 1600)
        self.assertNotIn("220 字以内", complete.call_args.args[0])
        self.assertNotIn("联网检索", complete.call_args.args[0])
        self.assertNotIn("联网检索", complete.call_args.args[1])
        self.assertIn("500 字以内", complete.call_args.args[0])

    def test_web_truncation_returns_explicit_rule_fallback(self):
        cfg = {"llm": {"api_key": "test-key", "model": "test-model", "web_search": True}}
        truncated = {"ok": False, "truncated": True, "error": "模型输出达到 token 上限"}
        with patch.object(chat, "_rule_answer", return_value={"text": "规则回答", "cites": []}), \
             patch.object(chat, "_augment_retrieval"), \
             patch.object(pipeline, "load_config", return_value=cfg), \
             patch.object(chat.LLM, "chat_web", return_value=truncated) as web:
            result = chat.answer("测试问题")

        self.assertEqual(result["text"], "规则回答")
        self.assertEqual(result["mode"], "rule")
        self.assertIn("模型输出未完成", result["note"])
        self.assertEqual(web.call_args.args[2], 1600)
    def test_failed_web_call_still_records_consumed_tokens(self):
        cfg = {"llm": {"api_key": "test-key", "model": "test-model", "web_search": True}}
        failed = {"ok": False, "truncated": True, "error": "模型输出达到 token 上限",
                  "usage": {"total_tokens": 123, "prompt_cache_hit_tokens": 23}}
        with patch.object(chat, "_rule_answer", return_value={"text": "规则回答", "cites": []}), \
             patch.object(chat, "_augment_retrieval"), \
             patch.object(pipeline, "load_config", return_value=cfg), \
             patch.object(chat.LLM, "chat_web", return_value=failed), \
             patch.object(chat.db, "add_tokens") as add_tokens:
            result = chat.answer("测试问题")

        self.assertEqual(result["mode"], "rule")
        add_tokens.assert_called_once_with(123, "chat", 23, 0)

    def test_normal_chat_exception_still_records_consumed_tokens(self):
        cfg = {"llm": {"api_key": "test-key", "model": "test-model", "web_search": False}}

        def fail(self, *args, **kwargs):
            self.tokens = 77
            self.cached_tokens = 11
            raise RuntimeError("upstream unavailable")

        with patch.object(chat, "_rule_answer", return_value={"text": "规则回答", "cites": []}), \
             patch.object(chat, "_augment_retrieval"), \
             patch.object(pipeline, "load_config", return_value=cfg), \
             patch.object(chat.LLM, "chat", fail), \
             patch.object(chat.db, "add_tokens") as add_tokens:
            result = chat.answer("测试问题")

        self.assertEqual(result["mode"], "rule")
        add_tokens.assert_called_once_with(77, "chat", 11, 0)


if __name__ == "__main__":
    unittest.main()
