"""更新检查回归测试：版本比对、清单校验、镜像回退、跳过版本、离线兜底。

跑法：python3 tests/test_updater.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import updater  # noqa: E402

GOOD = {
    "version": "1.9.7",
    "dmg_url": "https://example.com/llmintel/LLMIntelAgent-1.9.7.dmg",
    "sha256": "a" * 64,
    "size": 12345,
    "notes": "修复更新检查",
}


def reset(tmp, urls=None):
    os.environ.pop("LLMINTEL_UPDATE_URL", None)
    updater._state_file = lambda: os.path.join(tmp, "update_state.json")
    updater._STATE_CACHE = {"loaded": False, "last_check": 0.0, "manifest": None,
                            "skipped_version": ""}
    updater.current_version = lambda: "1.9.6"
    updater._read_user_config = lambda: {"update": {
        "manifest_url": (urls or ["https://primary/latest.json"])[0],
        "mirror_urls": (urls or [])[1:],
    }}


def test_version():
    assert updater._vtuple("1.9.6") == (1, 9, 6)
    assert updater._vtuple("v1.10.0") == (1, 10, 0)
    assert updater._vtuple("1.10.0") > updater._vtuple("1.9.9")
    assert updater._vtuple("2026-11-23 10:00") > (0, 0, 0)
    assert updater._vtuple("oops") == (0, 0, 0)


def test_validate():
    man = updater._validate_manifest(dict(GOOD))
    assert man["sha256"] == "a" * 64 and man["size"] == 12345
    bad = [
        {**GOOD, "sha256": "xyz"},
        {**GOOD, "size": 0},
        {**GOOD, "dmg_url": "https://"},
        {**GOOD, "dmg_url": "ftp://example.com/a.dmg"},
        {**GOOD, "dmg_url": "http://example.com/a.dmg"},
        {**GOOD, "version": "latest"},
    ]
    for m in bad:
        try:
            updater._validate_manifest(m)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝：{m}")
    # 本机 http 允许（本地联调）
    updater._validate_manifest({**GOOD, "dmg_url": "http://127.0.0.1:8000/a.dmg"})


def test_decide_and_skip():
    with tempfile.TemporaryDirectory() as tmp:
        reset(tmp)
        d = updater._decide(dict(GOOD))
        assert d["available"] and d["version"] == "1.9.7" and not d["skipped"]
        assert updater.skip("1.9.7")["ok"]
        d = updater._decide(dict(GOOD))
        assert not d["available"] and d["skipped"] and d["skipped_version"] == "1.9.7"
        # 跳过只针对该版本；更高版本仍提示
        d = updater._decide({**GOOD, "version": "1.10.0"})
        assert d["available"] and not d["skipped"]
        # mandatory 无视跳过
        d = updater._decide({**GOOD, "mandatory": True})
        assert d["available"]


def test_mirror_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        urls = ["https://down/latest.json", "https://mirror/latest.json"]
        reset(tmp, urls)
        calls = []

        def fake(url):
            calls.append(url)
            if "down" in url:
                raise OSError("connection refused")
            return updater._validate_manifest(dict(GOOD))

        updater._fetch_manifest = fake
        d = updater.check(force=True)
        assert d["ok"] and d["available"] and d["source"] == "https://mirror/latest.json"
        assert calls == urls


def test_offline_keeps_last_good():
    with tempfile.TemporaryDirectory() as tmp:
        reset(tmp)
        with open(os.path.join(tmp, "update_state.json"), "w", encoding="utf-8") as f:
            json.dump({"last_check": 1, "manifest": GOOD, "skipped_version": ""}, f)
        updater._STATE_CACHE = {"loaded": False, "last_check": 0.0, "manifest": None,
                                "skipped_version": ""}

        def boom(url):
            raise OSError("offline")

        updater._fetch_manifest = boom
        d = updater.check(force=True)
        assert d["ok"] is False and d["stale"] is True
        assert d["available"] and d["cached"] and d["version"] == "1.9.7"
        assert "offline" in d["error"]

        # 无历史清单时只报错，不伪造可更新
        updater._STATE_CACHE = {"loaded": False, "last_check": 0.0, "manifest": None,
                                "skipped_version": ""}
        os.remove(os.path.join(tmp, "update_state.json"))
        d = updater.check(force=True)
        assert d["ok"] is False and d["available"] is False


def main():
    tests = [test_version, test_validate, test_decide_and_skip,
             test_mirror_fallback, test_offline_keeps_last_good]
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
