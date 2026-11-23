"""Keychain 封装的降级行为（不碰真实 Keychain）。"""
import os
import sys

os.environ["LLMINTEL_KEYCHAIN"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import secrets_store

assert secrets_store.available() is False
assert secrets_store.get_key("active") == ""
assert secrets_store.set_key("active", "sk-test") is False
assert secrets_store.delete_key("active") is False
st = secrets_store.status()
assert st["service"] == "LLMIntelAgent" and st["available"] is False
print("PASS secrets_store fallback ->", st)
