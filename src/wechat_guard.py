"""公众号直链的限流守卫：识别「访问过于频繁」类页面，进入冷却期，避免越抓越封。

背景：mp.weixin.qq.com 对高频直连会返回验证/频繁提示。继续重试只会延长封禁，
所以一旦识别到，就停止本轮剩余文章，并写入冷却截止时间，下一轮自动跳过。
"""
import json
import os
import time

import db

# 微信侧的限制页/提示文案（命中任一即视为受限）
LIMIT_MARKS = (
    "访问过于频繁",
    "请求过于频繁",
    "操作频繁",
    "环境异常",
    "请在微信客户端打开",
    "请在微信中打开",
)
DEFAULT_HOURS = 6.0


class WechatRateLimited(Exception):
    """带已抓结果的限流异常：已成功的那几篇不浪费，剩余的停止。"""

    def __init__(self, msg: str = "公众号访问受限", fetched: int = 0, entries=None):
        super().__init__(msg)
        self.fetched = fetched
        self.entries = entries or []


def looks_limited(body: str, http_status: int | None = None) -> bool:
    if http_status == 429:
        return True
    s = (body or "")[:200000]
    # 正常文章一定有 js_content 容器；有这个就不按文案误判（避免文章正文讨论限流）
    if 'id="js_content"' in s or "id='js_content'" in s:
        return False
    return any(m in s for m in LIMIT_MARKS)


def _path() -> str:
    return os.path.join(db.app_dir(), "wechat_cooldown.json")


def _read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cooldown_status() -> dict:
    """{active, until, remaining_h, error}；到期自动视为未冷却。"""
    d = _read()
    until = float(d.get("until") or 0)
    now = time.time()
    left = max(0.0, until - now)
    return {
        "active": left > 0,
        "until": until,
        "until_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(until)) if until else "",
        "remaining_h": round(left / 3600, 1),
        "error": str(d.get("error") or ""),
    }


def set_cooldown(hours: float = DEFAULT_HOURS, error: str = "") -> dict:
    hours = max(0.5, min(float(hours or DEFAULT_HOURS), 24.0))
    until = time.time() + hours * 3600
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump({"until": until, "error": error[:200],
                       "set_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
    except Exception:
        pass
    return cooldown_status()


def clear() -> None:
    try:
        os.remove(_path())
    except Exception:
        pass
