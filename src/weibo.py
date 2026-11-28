"""微博本机只读通道：扫码登录 + 关键词搜索（方案二）。

实测（2026-11-28）：
  - 匿名搜索不可用：s.weibo.com 跳 Sina Visitor System，m.weibo.cn 返回 432
  - 扫码登录可用：passport.weibo.com/sso/v2/qrcode/image 返回 qrid + 扫码 URL
  - 登录后搜：m.weibo.cn/api/container/getIndex（containerid=100103type=1&q=关键词）

协议实现参考 qinyuanpei/mcp-server-weibo（MIT）与 RicterZ/mcp-server-weibo（MIT）。
登录态只存本机：macOS 写系统 Keychain，其余平台退回 600 权限文件。
"""
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import db
import ratelimit
import secrets_store

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")

SIGNIN_URL = "https://passport.weibo.com/sso/signin"
QR_IMAGE_URL = "https://passport.weibo.com/sso/v2/qrcode/image"
QR_CHECK_URL = "https://passport.weibo.com/sso/v2/qrcode/check"
SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"
CONFIG_URL = "https://m.weibo.cn/api/config"

PASSPORT_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": ("https://passport.weibo.com/sso/signin?"
                "entry=miniblog&source=miniblog&url=https://weibo.com/"),
    "X-Requested-With": "XMLHttpRequest",
}

# 默认检索词：智谱/唐杰为主，竞对补两条（太多关键词容易触发风控）
DEFAULT_KEYWORDS = ["智谱", "GLM", "唐杰", "DeepSeek", "Kimi", "豆包"]

_PENDING: dict[str, dict] = {}       # qrid -> {jar, at}（登录过程中临时态，不进磁盘）
_LOGIN_CACHE = {"at": 0.0, "val": None}


def get_mode() -> str:
    import pipeline
    cfg = pipeline.load_config()
    return (cfg.get("channels") or {}).get("weibo_mode", "off")


def keywords() -> list[str]:
    import pipeline
    cfg = pipeline.load_config()
    kw = (cfg.get("channels") or {}).get("weibo_keywords") or DEFAULT_KEYWORDS
    return [k for k in kw if (k or "").strip()][:6]


# ── 登录态存储 ──
def _cookie_file() -> str:
    d = db.app_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "weibo-session.json")


def load_cookies() -> dict:
    raw = ""
    if secrets_store.available():
        raw = secrets_store.get_key("weibo_session")
    if not raw:
        try:
            with open(_cookie_file(), encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            return {}
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in (data or {}).items() if k and v}
    except Exception:
        return {}


def save_cookies(cookies: dict) -> None:
    if not cookies:
        return
    raw = json.dumps(cookies, ensure_ascii=False)
    if secrets_store.available() and secrets_store.set_key("weibo_session", raw):
        try:                               # Keychain 成功后清掉旧文件，避免残留副本
            os.remove(_cookie_file())
        except OSError:
            pass
        return
    p = _cookie_file()
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    os.chmod(p, 0o600)


def clear_cookies() -> None:
    if secrets_store.available():
        secrets_store.delete_key("weibo_session")
    try:
        os.remove(_cookie_file())
    except OSError:
        pass
    _LOGIN_CACHE.update(at=0.0, val=None)


def _cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ── HTTP ──
def _open(url: str, jar: http.cookiejar.CookieJar | None = None,
          headers: dict | None = None, timeout: int = 15):
    h = {"User-Agent": MOBILE_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers or {})
    if jar is not None:
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
    else:
        op = urllib.request.build_opener()
    with ratelimit.IO_GATE:
        return op.open(urllib.request.Request(url, headers=h), timeout=timeout)


def _get_json(url: str, cookies: dict | None = None, headers: dict | None = None) -> dict:
    h = {"Accept": "application/json", "Referer": "https://m.weibo.cn/"}
    if cookies:
        h["Cookie"] = _cookie_header(cookies)
    h.update(headers or {})
    with ratelimit.IO_GATE:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))


def _jar_dict(jar: http.cookiejar.CookieJar) -> dict:
    return {c.name: c.value for c in jar}


def login_status(refresh: bool = False) -> dict:
    """校验本机登录态。20 秒内复用结果，避免 /api/channels 轮询打微博。"""
    if not refresh and time.time() - _LOGIN_CACHE["at"] < 20:
        return dict(_LOGIN_CACHE["val"] or {"logged_in": False, "uid": None})
    cookies = load_cookies()
    if not cookies:
        val = {"logged_in": False, "uid": None}
    else:
        try:
            d = _get_json(CONFIG_URL, cookies).get("data") or {}
            val = {"logged_in": bool(d.get("login")), "uid": d.get("uid") or None}
        except Exception:
            val = {"logged_in": False, "uid": None}
    _LOGIN_CACHE.update(at=time.time(), val=val)
    return dict(val)


# ── 扫码登录 ──
def qr_start() -> dict:
    """申请登录二维码。返回 {qrid, scan_url}；图片由前端用 /api/qr?text= 渲染。"""
    jar = http.cookiejar.CookieJar()
    _open(SIGNIN_URL + "?" + urllib.parse.urlencode({
        "entry": "miniblog", "source": "miniblog", "url": "https://weibo.com/"}),
        jar=jar, headers=PASSPORT_HEADERS)
    csrf = next((c.value for c in jar if c.name == "X-CSRF-TOKEN"), "")
    hdr = dict(PASSPORT_HEADERS)
    if csrf:
        hdr["X-CSRF-TOKEN"] = csrf
    r = _open(QR_IMAGE_URL + "?entry=miniblog&size=180", jar=jar, headers=hdr)
    data = json.loads(r.read().decode("utf-8", "ignore"))
    if data.get("retcode") != 20000000:
        raise RuntimeError(f"申请二维码失败：{data.get('msg') or data.get('retcode')}")
    qr = data.get("data") or {}
    qrid = qr.get("qrid")
    if not qrid:
        raise RuntimeError("二维码响应缺少 qrid")
    scan_url = urllib.parse.parse_qs(
        urllib.parse.urlparse(qr.get("image") or "").query).get("data", [None])[0]
    scan_url = scan_url or f"https://passport.weibo.cn/signin/qrcode/scan?qr={qrid}"
    _PENDING.clear()
    _PENDING[qrid] = {"jar": jar, "at": time.time()}
    return {"qrid": qrid, "scan_url": scan_url, "expires_in": 120}


def qr_poll(qrid: str) -> dict:
    """轮询扫码状态：waiting / scanned / expired / ok / error。"""
    p = _PENDING.get(qrid)
    if not p:
        return {"state": "expired", "msg": "二维码已失效，请刷新"}
    if time.time() - p["at"] > 300:
        _PENDING.pop(qrid, None)
        return {"state": "expired", "msg": "登录超时，请刷新二维码"}
    url = QR_CHECK_URL + "?" + urllib.parse.urlencode({
        "entry": "miniblog", "qrid": qrid, "rid": "", "ver": "20250520"})
    try:
        r = _open(url, jar=p["jar"], headers=PASSPORT_HEADERS)
        d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return {"state": "error", "msg": f"查询失败：{str(e)[:80]}"}

    code = d.get("retcode")
    if code == 50114001:
        return {"state": "waiting", "msg": "等待扫码"}
    if code == 50114002:
        return {"state": "scanned", "msg": "已扫码，请在手机上确认登录"}
    if code == 50114004:
        _PENDING.pop(qrid, None)
        return {"state": "expired", "msg": "二维码已过期，请刷新"}
    if code != 20000000:
        return {"state": "error", "msg": f"未知状态：{d.get('msg') or code}"}

    data = d.get("data") or {}
    try:
        if data.get("url"):
            _open(data["url"], jar=p["jar"], headers=PASSPORT_HEADERS)
        elif data.get("alt"):
            _open("https://passport.weibo.com/sso/v2/login?" + urllib.parse.urlencode({
                "entry": "miniblog", "alt": data["alt"], "returntype": "META"}),
                jar=p["jar"], headers=PASSPORT_HEADERS)
        else:
            return {"state": "error", "msg": "登录响应缺少跳转地址"}
    except Exception as e:
        return {"state": "error", "msg": f"登录跳转失败：{str(e)[:80]}"}

    cookies = _jar_dict(p["jar"])
    if not cookies.get("SUB"):
        return {"state": "error", "msg": "未拿到 SUB 登录票据"}
    save_cookies(cookies)
    _PENDING.pop(qrid, None)
    st = login_status(refresh=True)
    if not st.get("logged_in"):
        return {"state": "error", "msg": "登录票据无效，请重试"}
    return {"state": "ok", "uid": st.get("uid")}


# ── 搜索 ──
def _mblog_time(s: str) -> str:
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain_text(html: str) -> str:
    from collector import html_to_text
    return re.sub(r"\s+", " ", html_to_text(html or "", 4000)).strip()


def _feed_entry(mblog: dict) -> dict | None:
    mid = mblog.get("id")
    text = _plain_text(mblog.get("text") or "")
    if not mid or not text:
        return None
    user = (mblog.get("user") or {}).get("screen_name") or ""
    return {
        "title": f"[微博] {text[:80]}",
        "link": f"https://m.weibo.cn/detail/{mid}",
        "desc": f"@{user}｜{text[:280]}" if user else text[:280],
        "guid": f"weibo-{mid}",
        "t_valid": _mblog_time(mblog.get("created_at") or ""),
        "_body": text,
        "_source_name": f"微博·{user}" if user else "微博",
    }


def search(keyword: str, limit: int = 15, pages: int = 1) -> list[dict]:
    """关键词搜索。未登录/接口异常返回 []（由 probe 说明原因）。"""
    cookies = load_cookies()
    if not cookies or not (keyword or "").strip():
        return []
    out = []
    for page in range(1, max(1, pages) + 1):
        params = urllib.parse.urlencode({
            "containerid": f"100103type=1&q={keyword}",
            "page_type": "searchall", "page": page})
        try:
            d = _get_json(f"{SEARCH_URL}?{params}", cookies)
        except Exception:
            break
        if d.get("ok") != 1:
            break
        cards = (d.get("data") or {}).get("cards") or []
        got = 0
        for card in cards:
            if not isinstance(card, dict):
                continue
            group = card.get("card_group") if isinstance(card.get("card_group"), list) else []
            for item in [card] + group:
                if not isinstance(item, dict) or item.get("card_type") != 9:
                    continue
                e = _feed_entry(item.get("mblog") or {})
                if e:
                    out.append(e)
                    got += 1
                    if len(out) >= limit:
                        return out
        if not got:
            break
        time.sleep(2.0)
    return out[:limit]


def hot(limit: int = 20) -> list[dict]:
    """微博热搜（登录态下）。只保留与 AI 产业链相关的条目。"""
    cookies = load_cookies()
    if not cookies:
        return []
    try:
        d = _get_json(f"{SEARCH_URL}?" + urllib.parse.urlencode({
            "containerid": "106003type=25&t=3&disable_hot=1&filter_type=realtimehot"}),
            cookies)
    except Exception:
        return []
    items = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("desc") and o.get("scheme"):
                items.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk((d.get("data") or {}).get("cards") or [])
    import sources_api
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, seen = [], set()
    for it in items:
        word = re.sub(r"\s+", " ", str(it.get("desc") or "")).strip()
        if not word or word in seen:
            continue
        if not sources_api.entity_hit(word):
            continue
        seen.add(word)
        out.append({
            "title": f"[微博热搜] {word}",
            "link": (f"https://s.weibo.com/weibo?q=%23{urllib.parse.quote(word)}%23"),
            "desc": word,
            "guid": f"weibo-hot-{abs(hash(word)) % (10 ** 12)}",
            "t_valid": now,
            "_source_name": "微博热搜",
            "_no_fetch": True,
        })
        if len(out) >= limit:
            break
    return out


def fetch_login(limit_per: int = 15) -> list[dict]:
    """供 pipeline 采集：按关键词轮询搜索 + 热搜，跨轮去重。"""
    if not login_status().get("logged_in"):
        return []
    seen, out = set(), []
    for i, kw in enumerate(keywords()):
        if i:
            time.sleep(3.5)
        for e in search(kw, limit=limit_per):
            if e["guid"] in seen:
                continue
            seen.add(e["guid"])
            out.append(e)
    time.sleep(3.5)
    for e in hot(limit=10):
        if e["guid"] in seen:
            continue
        seen.add(e["guid"])
        out.append(e)
    return out


def probe() -> dict:
    mode = get_mode()
    if mode == "off":
        return {"status": "unavailable", "reason": "通道关闭（off）",
                "hint": "点卡片用微博 App 扫码登录后开启"}
    st = login_status()
    if not st.get("logged_in"):
        return {"status": "need_login", "reason": "尚未扫码登录",
                "hint": "点卡片，用微博 App 扫码；登录态只存本机"}
    return {"status": "ok", "reason": f"已登录（UID {st.get('uid') or '—'}）",
            "hint": None, "keywords": keywords()}
