"""小红书三面板 / 采集解析回归测试。"""
import sys, os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import social

WORDS = {
    "zhipu": ("智谱", "glm", "chatglm", "zhipu", "唐杰"),
    "tangjie": ("唐杰", "张鹏"),
    "competitor": ("deepseek", "kimi", "minimax", "qwen", "豆包"),
}

today = datetime.now().strftime("%Y-%m-%d")
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _patch_cache():
    social.get_mode = lambda: "local"
    social.sidecar_alive = lambda: True
    social.read_cache = lambda: {
        "at": "2026-11-23 06:00:00",
        "comments": [
            {"note_id": "n1", "note_title": "智谱 GLM 新版本体验", "note_url": "u1",
             "text": "GLM 写代码比上次稳，但还是有点慢", "likes": 8, "t": today,
             "day": today, "panel": "zhipu"},
            {"note_id": "n2", "note_title": "唐杰在清华的分享", "note_url": "u2",
             "text": "唐杰讲得挺清楚，智谱路线也解释了", "likes": 3, "t": yesterday,
             "day": yesterday, "panel": "tangjie"},
            {"note_id": "n3", "note_title": "DeepSeek 和 Kimi 怎么选", "note_url": "u3",
             "text": "DeepSeek 便宜但 Kimi 长文本更顺手", "likes": 12, "t": today,
             "day": today, "panel": "competitor"},
        ],
    }
    social._panel_words = lambda: WORDS


def test_panel_for_text():
    assert social.panel_for_text("唐杰在智谱的发言", WORDS) == "tangjie"
    assert social.panel_for_text("智谱 GLM 发布", WORDS) == "zhipu"
    assert social.panel_for_text("DeepSeek 新模型", WORDS) == "competitor"
    assert social.panel_for_text("一条无关内容", WORDS) == ""


def test_comment_report_panels():
    _patch_cache()
    z = social.comment_report(days=30, panel="zhipu")
    assert z["status"] == "ok" and z["panel"] == "zhipu"
    assert all(r["panel"] == "zhipu" for r in z["samples"])
    c = social.comment_report(days=30, panel="competitor")
    assert c["status"] == "ok" and all(r["panel"] == "competitor" for r in c["samples"])
    a = social.comment_report(days=30, panel="all")
    assert a["status"] == "ok" and len(a["samples"]) == 3
    assert a["panels"]["zhipu"] == 1 and a["panels"]["competitor"] == 1


def test_find_feeds_and_comments():
    resp = {"data": {"items": [
        {"note_id": "abc", "xsecToken": "tk1", "title": "t1"},
        {"note": {"id": "def", "xsec_token": "tk2"}},
        {"note_id": "dup", "xsecToken": "tk1"},
    ]}}
    feeds = social._find_feeds(resp)
    ids = {f["id"] for f in feeds}
    assert ids == {"abc", "def"}
    detail = {"comments": [
        {"content": "好用，推荐", "likeCount": "5", "userInfo": {"nickname": "a"}},
        {"content": "太慢了", "likedCount": 2},
        {"foo": "no text"},
    ]}
    cs = social._scan_comments(detail)
    assert len(cs) == 2


def test_collect_state_panels():
    _patch_cache()
    plan = [{"kw": "智谱", "panel": "zhipu"}, {"kw": "唐杰", "panel": "tangjie"},
            {"kw": "DeepSeek", "panel": "competitor"}]
    social.keyword_plan = lambda: plan
    st = social.collect_state()
    assert st["keyword_count"] == 3
    assert st["cache_panels"]["zhipu"] == 1
    assert st["cache_panels"]["tangjie"] == 1


def main():
    tests = [test_panel_for_text, test_comment_report_panels,
             test_find_feeds_and_comments, test_collect_state_panels]
    bad = 0
    for t in tests:
        try:
            t()
            print("PASS " + t.__name__)
        except Exception as e:
            bad += 1
            print("FAIL " + t.__name__ + "  ->  " + repr(e))
    print(f"\n{len(tests)-bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
