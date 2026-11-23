"""公众号限流守卫测试：识别提示页、冷却写入/读取/清除。

跑法：python3 tests/test_wechat_guard.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import wechat_guard  # noqa: E402


def test_detect():
    assert wechat_guard.looks_limited("访问过于频繁，请稍后再试")
    assert wechat_guard.looks_limited("当前环境异常，完成验证后即可继续访问")
    assert wechat_guard.looks_limited("请在微信客户端打开链接")
    assert wechat_guard.looks_limited("", http_status=429)
    assert not wechat_guard.looks_limited("<html>正常文章，智谱发布 GLM-5</html>")
    assert not wechat_guard.looks_limited("", http_status=404)
    # 正常文章正文里讨论「访问过于频繁」不应误判
    assert not wechat_guard.looks_limited(
        '<div id="js_content">今天我们聊聊「访问过于频繁」的限流设计</div>')


def test_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        wechat_guard.db.app_dir = lambda: tmp
        st = wechat_guard.cooldown_status()
        assert not st["active"] and st["until"] == 0
        wechat_guard.set_cooldown(1, "HTTP 429")
        st = wechat_guard.cooldown_status()
        assert st["active"] and st["remaining_h"] <= 1.0
        assert st["until_text"] and st["error"] == "HTTP 429"
        assert os.path.exists(os.path.join(tmp, "wechat_cooldown.json"))
        wechat_guard.clear()
        assert not wechat_guard.cooldown_status()["active"]


def test_exception_carries_entries():
    e = wechat_guard.WechatRateLimited("限流", fetched=2, entries=[{"title": "a"}, {"title": "b"}])
    assert e.fetched == 2 and len(e.entries) == 2


def main():
    tests = [test_detect, test_cooldown, test_exception_carries_entries]
    bad = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:
            bad += 1
            print("FAIL", t.__name__, "→", type(e).__name__, e)
    print(f"\n{len(tests)-bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
