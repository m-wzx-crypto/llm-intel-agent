"""LLM 输出完成状态回归测试。"""
import json
import os
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import extract


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class CompletionTests(unittest.TestCase):
    def test_chat_web_marks_partial_output_as_truncated(self):
        response = FakeResponse({
            "choices": [{
                "message": {"content": "回答只生成了一半……"},
                "finish_reason": "length",
            }],
            "usage": {},
        })
        llm = extract.LLM({"api_key": "test-key", "model": "test-model"})
        with patch.object(extract.urllib.request, "urlopen", return_value=response), \
             patch.object(extract.ratelimit, "check_budget", return_value=None), \
             patch.object(extract.ratelimit, "LLM_GATE", nullcontext()):
            result = llm.chat_web("system", "user")

        self.assertFalse(result["ok"])
        self.assertTrue(result["truncated"])

    def test_chat_retries_when_partial_output_hits_token_limit(self):
        responses = [
            FakeResponse({
                "choices": [{
                    "message": {"content": "半截回答"},
                    "finish_reason": "length",
                }],
                "usage": {},
            }),
            FakeResponse({
                "choices": [{
                    "message": {"content": "完整回答"},
                    "finish_reason": "stop",
                }],
                "usage": {},
            }),
        ]
        llm = extract.LLM({"api_key": "test-key", "model": "test-model"})
        with patch.object(extract.urllib.request, "urlopen", side_effect=responses) as request, \
             patch.object(extract.ratelimit, "check_budget", return_value=None), \
             patch.object(extract.ratelimit, "LLM_GATE", nullcontext()):
            text = llm.chat("system", "user", max_tokens=100)

        self.assertEqual(text, "完整回答")
        self.assertEqual(request.call_count, 2)

    def test_chat_raises_when_all_attempts_are_truncated(self):
        responses = [FakeResponse({
            "choices": [{
                "message": {"content": "仍然只有半截"},
                "finish_reason": "length",
            }],
            "usage": {},
        }) for _ in range(3)]
        llm = extract.LLM({"api_key": "test-key", "model": "test-model"})
        with patch.object(extract.urllib.request, "urlopen", side_effect=responses) as request, \
             patch.object(extract.ratelimit, "check_budget", return_value=None), \
             patch.object(extract.ratelimit, "LLM_GATE", nullcontext()):
            with self.assertRaisesRegex(RuntimeError, "模型输出未完成"):
                llm.chat("system", "user", max_tokens=100)

        self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
