"""社媒适配器：小红书三档状态机（off / local / vendor）。

设计原则（技术方案 §9）：
  - 默认 off。直连需登录态 + 请求签名，有判例风险，不预置。
  - local：内置 xiaohongshu-mcp（Apache-2.0，随 app 分发），客户在自己机器上扫码授权，
           采集只在本机、凭据不出网。首次启动会自动下载其内置浏览器（约 180MB）。
  - vendor：客户自购商业数据源（千瓜/新榜等），我们只消费其导出/API。

所以这个模块当前的作用是：**把"能不能采"变成数据**，而不是让 UI 假装有数据。
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

import db

MODES = {
    "off": "不采集（默认）。硬采不成时 UI 会把它伪装成「全网没有讨论」，所以宁可空着并标注。",
    "local": "本机扫码只读：内置 xiaohongshu-mcp（127.0.0.1:18060，随 app 分发），用客户自己的账号登录后读页面。",
    "vendor": "客户自购商业数据源（千瓜/新榜等），由外部导入。",
}

# 扫码入口：只指向「去哪申请/导出」，不包装成授权按钮。
# 扫码本身不建立任何数据连接——小红书没有公开的评论读取接口。
CHANNELS = [
    {"id": "open", "name": "小红书开放平台", "url": "https://open.xiaohongshu.com/",
     "desc": "官方入口。扫码去申请开发者资质；授权后仍需在本应用配置 app_id/secret 才可能取数（本版未内置 OAuth）。"},
    {"id": "qian", "name": "千瓜数据", "url": "https://www.qian-gua.com/",
     "desc": "商业数据源（vendor）。导出笔记与评论 JSON，放进本机 xhs/ 目录即可。"},
    {"id": "newrank", "name": "新榜", "url": "https://www.newrank.cn/",
     "desc": "商业数据源（vendor）。同上，导出 JSON 后放入 xhs/ 目录。"},
]

# 三面板关键词：竞对不再只搜「智谱/GLM/唐杰」，而是把竞对也纳入同一轮采集。
# 顺序即优先级；max_keywords 控制单轮耗时（每关键词至少一次搜索 + N 次详情）。
DEFAULT_PANELS = {
    "zhipu": ["智谱", "GLM", "ChatGLM"],
    "tangjie": ["唐杰", "张鹏"],
    "competitor": ["DeepSeek", "Kimi", "MiniMax", "Qwen", "豆包", "OpenAI",
                   "Gemini", "宇树", "智元", "英伟达"],
}
PANEL_LABEL = {"zhipu": "智谱", "tangjie": "唐杰", "competitor": "竞对"}

# ── local 通道：本机 xiaohongshu-mcp sidecar（REST，默认 127.0.0.1:18060）──
# 不打包对方代码、不做签名逆向：只做「检测到就在用」，数据经客户自己扫码登录的账号读取。
XHS_BASE = os.environ.get("XHS_MCP_BASE", "http://127.0.0.1:18060").rstrip("/")
_COLLECT = {"running": False, "msg": ""}


def _channels_cfg() -> dict:
    try:
        import pipeline
        return pipeline.load_config().get("channels") or {}
    except Exception:
        return {}


def keyword_plan() -> list[dict]:
    """展开配置里的三面板关键词，去重并按上限截断。

    按面板轮转排序（智谱/唐杰/竞对各取一个，再取第二个…），
    这样定时任务用较小上限截断时，三块面板都能被覆盖到。
    """
    cfg = _channels_cfg()
    panels = cfg.get("xiaohongshu_panels") or DEFAULT_PANELS
    try:
        cap = int(cfg.get("xiaohongshu_max_keywords") or 15)
    except Exception:
        cap = 15
    cap = max(1, min(cap, 30))
    order = ("zhipu", "tangjie", "competitor")
    lists = {p: [str(k or "").strip() for k in (panels.get(p) or [])] for p in order}
    out, seen, i = [], set(), 0
    while any(i < len(lists[p]) for p in order):
        for panel in order:
            if i < len(lists[panel]):
                kw = lists[panel][i]
                key = kw.lower()
                if kw and key not in seen:
                    seen.add(key)
                    out.append({"kw": kw, "panel": panel})
                    if len(out) >= cap:
                        return out
        i += 1
    return out


def keywords() -> list[str]:
    return [x["kw"] for x in keyword_plan()]


def _panel_words() -> dict[str, tuple[str, ...]]:
    """面板 → 命中词。zhipu 保留唐杰/张鹏，兼容旧口径。"""
    out = {"zhipu": list(_ZP_WORDS), "tangjie": ["唐杰", "张鹏"], "competitor": []}
    for x in keyword_plan():
        out.setdefault(x["panel"], []).append(x["kw"].lower())
    return {k: tuple(dict.fromkeys(v)) for k, v in out.items()}


def panel_for_text(blob: str, words: dict[str, tuple[str, ...]] | None = None) -> str:
    """按「唐杰 → 智谱 → 竞对」顺序判定一条内容属于哪个面板。"""
    blob = (blob or "").lower()
    words = words or _panel_words()
    for p in ("tangjie", "zhipu", "competitor"):
        if any(w in blob for w in words.get(p, ())):
            return p
    return ""


def _req(method: str, path: str, body: dict | None = None, timeout: float = 20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(XHS_BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception:
        return None
    try:
        return json.loads(raw or b"{}")
    except Exception:
        return {"raw": (raw or b"").decode("utf-8", "ignore")[:400]}


def sidecar_alive() -> bool:
    return _req("GET", "/health", timeout=2) is not None


def login_status() -> dict:
    """容忍各种返回结构：找第一个名字含 logged 的布尔值。"""
    data = _req("GET", "/api/v1/login/status", timeout=8) or {}
    hits = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, bool) and "logged" in k.lower():
                    hits[k.lower()] = v
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return {"ok": bool(data), "logged_in": any(hits.values()), "raw": hits}


def login_qrcode() -> dict:
    data = _req("GET", "/api/v1/login/qrcode", timeout=15) or {}
    img = {"v": None}

    def walk(o):
        if img["v"]:
            return
        if isinstance(o, str):
            if o.startswith("data:image"):
                img["v"] = o
            elif len(o) > 2000 and not o.startswith("http"):
                img["v"] = "data:image/png;base64," + o.strip()
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return {"ok": bool(img["v"]), "image": img["v"]}


def _cache_path() -> str:
    return os.path.join(db.app_dir(), "xhs_cache.json")


def read_cache() -> dict:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cache(obj: dict) -> None:
    tmp = _cache_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, _cache_path())


def _ts_day(v) -> str:
    s = str(v or "").strip()
    if s.isdigit():
        n = int(s)
        if n > 10 ** 12:
            n //= 1000
        try:
            return datetime.fromtimestamp(n).strftime("%Y-%m-%d")
        except Exception:
            return ""
    return _day(s)


def _deep_get(o, keys):
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in keys and isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


def _find_feeds(obj) -> list[dict]:
    """从搜索响应里找「带 xsec_token 的笔记」，不依赖固定层级。"""
    out, seen, seen_tok = [], set(), set()

    def walk(o):
        if isinstance(o, dict):
            tok = o.get("xsecToken") or o.get("xsec_token")
            if isinstance(tok, str) and tok:
                fid = _deep_get(o, ("note_id", "noteId", "feed_id", "feedId", "id"))
                if fid and fid not in seen and tok not in seen_tok:
                    seen.add(fid)
                    seen_tok.add(tok)
                    out.append({"id": fid, "token": tok})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return out


def _note_title(obj) -> str:
    keys = ("title", "displayTitle", "display_title", "desc", "content")
    found = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys and isinstance(v, str) and v.strip() and k not in found:
                    found[k] = v.strip()
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    for k in keys:
        if found.get(k):
            return found[k][:60]
    return ""


def _scan_comments(obj) -> list[dict]:
    """评论字段名随版本会漂：按「有文本 + 有点赞/用户」的特征收集。"""
    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            txt = o.get("content") or o.get("text")
            likes = o.get("likeCount", o.get("like_count", o.get("likedCount")))
            user = o.get("userInfo") or o.get("user_info") or o.get("user")
            if isinstance(txt, str) and txt.strip() and (likes is not None or user is not None):
                key = txt.strip()[:120]
                if key not in seen:
                    seen.add(key)
                    out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return out


def collect(limit_per_kw: int | None = None, keywords=None,
            comments_per_note: int | None = None, detail_timeout: float = 120) -> dict:
    """跑一次本机采集：搜索关键词 → 读笔记详情（含评论）→ 落 xhs_cache.json。

    关键词默认为三面板配置（智谱/唐杰/竞对），笔记数与评论数可在设置里调；
    detail_timeout 给定时任务用较小值，避免单条笔记把整轮拖死。
    """
    if _COLLECT["running"]:
        return {"ok": False, "reason": "正在采集"}
    cfg = _channels_cfg()
    if limit_per_kw is None:
        try:
            limit_per_kw = int(cfg.get("xiaohongshu_notes_per_keyword") or 4)
        except Exception:
            limit_per_kw = 4
    if comments_per_note is None:
        try:
            comments_per_note = int(cfg.get("xiaohongshu_comments_per_note") or 20)
        except Exception:
            comments_per_note = 20
    limit_per_kw = max(1, min(int(limit_per_kw), 20))
    comments_per_note = max(1, min(int(comments_per_note), 100))
    detail_timeout = max(15, min(float(detail_timeout), 300))

    if keywords:
        plan = []
        for k in keywords:
            if isinstance(k, dict):
                kw = str(k.get("kw") or k.get("keyword") or "").strip()
                plan.append({"kw": kw, "panel": str(k.get("panel") or "")})
            else:
                kw = str(k or "").strip()
                plan.append({"kw": kw, "panel": ""})
        plan = [p for p in plan if p["kw"]]
    else:
        plan = keyword_plan()

    _COLLECT.update(running=True, msg="检查登录")
    try:
        if not sidecar_alive():
            return {"ok": False, "reason": "本机通道未启动（xiaohongshu-mcp 未运行）"}
        if not login_status().get("logged_in"):
            return {"ok": False, "reason": "未登录", "need_login": True}
        records, notes, failed = [], [], []
        for item in plan:
            kw = item["kw"]
            panel = item.get("panel") or panel_for_text(kw) or "competitor"
            _COLLECT["msg"] = f"搜索「{kw}」"
            res = _req("GET", "/api/v1/feeds/search?keyword=" + urllib.parse.quote(kw),
                       timeout=90)
            for f in _find_feeds(res or {})[:limit_per_kw]:
                _COLLECT["msg"] = f"读笔记 {f['id'][:8]}…"
                detail = _req("POST", "/api/v1/feeds/detail", {
                    "feed_id": f["id"], "xsec_token": f["token"],
                    "load_all_comments": True,
                    "comment_config": {"load_all_comments": True,
                                       "max_comment_items": comments_per_note,
                                       "click_more_replies": False,
                                       "max_replies_threshold": 10,
                                       "scroll_speed": "normal"},
                }, timeout=detail_timeout)
                if not detail:
                    failed.append(f["id"])
                    continue
                title = _note_title(detail) or kw
                url = (f"https://www.xiaohongshu.com/explore/{f['id']}"
                       f"?xsec_token={f['token']}&xsec_source=pc_search")
                notes.append({"note_id": f["id"], "title": title, "url": url,
                              "panel": panel, "kw": kw})
                for c in _scan_comments(detail):
                    txt = (c.get("content") or c.get("text") or "").strip()
                    if not txt:
                        continue
                    likes = c.get("likeCount", c.get("like_count",
                                                        c.get("likedCount", 0)))
                    try:
                        likes = int(likes)
                    except Exception:
                        likes = 0
                    ts = c.get("createTime") or c.get("create_time") or c.get("time")
                    records.append({
                        "note_id": f["id"], "note_title": title, "note_url": url,
                        "text": txt[:500], "likes": likes, "t": str(ts or ""),
                        "day": _ts_day(ts), "panel": panel, "kw": kw,
                    })
        uniq = {(r["note_id"], r["text"]): r for r in records}
        records = list(uniq.values())

        # 与上次缓存合并：每日只跑少量关键词，不能把别的面板（尤其竞对）冲掉。
        # 同一笔记同一评论以本次为准；上限 3000 条，超出丢最旧的。
        old = read_cache()
        merged = {(r.get("note_id"), r.get("text")): r
                  for r in (old.get("comments") or []) if r.get("text")}
        for r in records:
            merged[(r["note_id"], r["text"])] = r
        merged_records = list(merged.values())[-3000:]
        notes_map = {n.get("note_id"): n for n in (old.get("notes") or []) if n.get("note_id")}
        for n in notes:
            notes_map[n["note_id"]] = n
        merged_notes = list(notes_map.values())[-1000:]

        counts = {p: 0 for p in PANEL_LABEL}
        for r in merged_records:
            if r.get("panel") in counts:
                counts[r["panel"]] += 1
        _write_cache({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "source": "xiaohongshu-mcp", "keywords": plan,
                      "notes": merged_notes, "comments": merged_records, "panels": counts,
                      "last_run": {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   "keywords": [p["kw"] for p in plan],
                                   "comments": len(records)}})
        return {"ok": True, "notes": len(notes), "comments": len(records),
                "cache_comments": len(merged_records),
                "failed": failed[:5], "panels": counts,
                "keywords": [p["kw"] for p in plan]}
    finally:
        _COLLECT.update(running=False, msg="")


def collect_state() -> dict:
    c = read_cache()
    plan = keyword_plan()
    counts = {p: 0 for p in PANEL_LABEL}
    for r in (c.get("comments") or []):
        p = r.get("panel") or panel_for_text(f"{r.get('note_title','')} {r.get('text','')}")
        if p in counts:
            counts[p] += 1
    return {"running": _COLLECT["running"], "msg": _COLLECT["msg"],
            "cache_at": c.get("at"), "cache_comments": len(c.get("comments") or []),
            "cache_notes": len(c.get("notes") or []),
            "cache_panels": c.get("panels") or counts,
            "last_run": c.get("last_run") or {},
            "keywords": [p["kw"] for p in plan],
            "keyword_count": len(plan)}


def get_mode() -> str:
    import pipeline
    cfg = pipeline.load_config()
    return (cfg.get("channels") or {}).get("xiaohongshu_mode", "off")


def probe() -> dict:
    """返回该通道的健康状态（四态之一）+ 说明，供 probe_run / UI 使用"""
    mode = get_mode()
    if mode == "off":
        return {"status": "unavailable", "reason": "通道关闭（off）",
                "hint": "如需要，可切到 local（本机扫码）或 vendor（商业数据源）"}
    if mode == "local":
        if not sidecar_alive():
            try:
                import xhs_sidecar
                en = xhs_sidecar.state()
            except Exception:
                en = {}
            if en.get("starting"):
                return {"status": "starting", "reason": "内置本机通道启动中",
                        "hint": "首次启动会下载内置浏览器（约 180MB），完成后自动出现登录二维码"}
            return {"status": "unavailable",
                    "reason": en.get("error") or "本机通道未启动",
                    "hint": "点卡片可启动/重启内置服务（无需另行安装）"}
        if not login_status().get("logged_in"):
            return {"status": "need_login", "reason": "本机通道已连接，等待扫码登录",
                    "hint": "打开评论区卡片，用小红书 App 扫码"}
        st = collect_state()
        if not st["cache_comments"]:
            return {"status": "need_collect", "reason": "已登录，尚未采集",
                    "hint": f"上次采集：{st['cache_at'] or '从未'}，点卡片里的「开始采集」",
                    "keywords": st["keywords"]}
        return {"status": "ok", "reason": f"本机缓存 {st['cache_notes']} 条笔记 / "
                                          f"{st['cache_comments']} 条评论（{st['cache_at']}）",
                "hint": "数据为上次采集结果，可重新采集",
                "keywords": st["keywords"], "panels": st["cache_panels"]}
    if mode == "vendor":
        rows = _vendor_rows()
        cs = list(_iter_comments())
        if not rows and not cs:
            return {"status": "zero_hit", "reason": "vendor 模式已开启，但还没有导入任何数据",
                    "hint": "把商业数据源导出的 JSON 放到数据目录 xhs/ 下"}
        return {"status": "ok", "reason": f"vendor 数据 {len(rows)} 条笔记 / {len(cs)} 条评论",
                "hint": None}
    return {"status": "unavailable", "reason": f"未知模式 {mode}"}


def _vendor_dir() -> str:
    d = os.path.join(db.app_dir(), "xhs")
    os.makedirs(d, exist_ok=True)
    return d


def _vendor_data() -> list:
    """读 xhs/ 下所有 vendor JSON（原样返回，不假设结构）"""
    out = []
    d = _vendor_dir()
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            out.append(json.load(open(os.path.join(d, fn), encoding="utf-8")))
        except Exception:
            continue
    return out


def _vendor_rows() -> list[dict]:
    """笔记列表（兼容 {items:[...]} / {notes:[...]} / [...]）"""
    out = []
    for data in _vendor_data():
        if isinstance(data, dict):
            data = data.get("items") or data.get("notes") or []
        if not isinstance(data, list):
            continue
        out.extend([x for x in data if isinstance(x, dict)])
    return out


_ZP_WORDS = ("智谱", "glm", "chatglm", "zhipu", "唐杰")
_TOPIC_WORDS = ("价格", "收费", "免费", "会员", "额度", "降智", "幻觉", "卡顿", "封号",
                "退款", "客服", "好用", "难用", "垃圾", "支持", "劝退", "智商税",
                "平替", "对比", "编程", "写代码", "翻译", "长文本", "多模态", "开源")
# 评论区是口语，不能用新闻标题词典（新闻里的「提升/下滑」在评论里含义不同）
_POS_WORDS = ("好用", "好使", "真香", "流畅", "稳定", "准确", "厉害", "牛", "强大",
              "推荐", "满意", "方便", "高效", "提升", "省钱", "值", "赞", "喜欢",
              "惊艳", "性价比", "靠谱", "快")
_NEG_WORDS = ("难用", "卡顿", "太慢", "垃圾", "劝退", "降智", "幻觉", "答非所问",
              "太贵", "涨价", "扣费", "退款", "封号", "崩溃", "报错", "不准",
              "失望", "智商税", "割韭菜", "客服差", "额度少", "限制", "难用", "坑")


def _norm_comment(c: dict, note: dict | None = None) -> dict | None:
    if not isinstance(c, dict):
        return None
    text = c.get("text") or c.get("content") or c.get("comment") or ""
    if not text:
        return None
    likes = c.get("likes") or c.get("like_count") or c.get("like") or 0
    try:
        likes = int(likes)
    except Exception:
        likes = 0
    note = note or {}
    return {
        "note_id": str(note.get("note_id") or c.get("note_id") or ""),
        "note_title": str(note.get("title") or note.get("desc") or ""),
        "note_url": note.get("url") or note.get("note_url") or "",
        "text": str(text)[:500],
        "likes": likes,
        "t": str(c.get("time") or c.get("publish_time") or c.get("create_time") or ""),
    }


def _iter_comments():
    """兼容三种导出：笔记内嵌 comments[]；顶层 comments[]；独立评论列表"""
    for data in _vendor_data():
        if isinstance(data, dict):
            notes = data.get("items") or data.get("notes") or []
            if isinstance(notes, list):
                for n in notes:
                    if not isinstance(n, dict):
                        continue
                    for c in (n.get("comments") or []):
                        r = _norm_comment(c, n)
                        if r:
                            yield r
            for c in (data.get("comments") or []):
                r = _norm_comment(c)
                if r:
                    yield r
        elif isinstance(data, list):
            looks_like_comments = (data and not any("title" in x for x in data if isinstance(x, dict))
                                   and any("text" in x or "comment" in x for x in data
                                           if isinstance(x, dict)))
            if looks_like_comments:
                for c in data:
                    r = _norm_comment(c)
                    if r:
                        yield r
            else:
                for n in data:
                    if not isinstance(n, dict):
                        continue
                    for c in (n.get("comments") or []):
                        r = _norm_comment(c, n)
                        if r:
                            yield r


def _day(s: str) -> str:
    s = (s or "").strip()
    return s[:10] if len(s) >= 10 and s[4:5] in ("-", "/") else ""


def comment_report(days: int = 90, q: str | None = None, panel: str = "zhipu") -> dict:
    """小红书评论区风向。默认只看智谱面板；competitor/all 可看竞对或全量。

    指标刻意只报结构性信号：样本量、正/中/负占比、分歧指数、话题词命中。
    情感用词典判定，明确标注会误判反讽；没有数据时返回 zero_hit，不冒充「没人讨论」。
    """
    mode = get_mode()
    if mode == "local":
        if not sidecar_alive():
            try:
                import xhs_sidecar
                en = xhs_sidecar.state()
            except Exception:
                en = {}
            starting = bool(en.get("starting"))
            return {"status": "starting" if starting else "unavailable", "mode": mode,
                    "samples": 0, "sidecar": False,
                    "reason": ("内置本机通道启动中" if starting
                               else (en.get("error") or "本机通道未启动")),
                    "hint": ("首次启动会下载内置浏览器（约 180MB），完成后自动出现二维码"
                             if starting else "点卡片可启动/重启内置服务（无需另行安装）"),
                    "channels": CHANNELS, "import_dir": _vendor_dir(),
                    "counts": {"notes": 0, "comments": 0}}
        cache = read_cache()
        if not cache.get("comments"):
            logged = login_status().get("logged_in")
            return {"status": "need_collect" if logged else "need_login", "mode": mode,
                    "samples": 0, "sidecar": True, "logged_in": logged,
                    "reason": "已登录，尚未采集" if logged else "本机通道已连接，等待扫码登录",
                    "hint": "点卡片里的「开始采集」" if logged else "用小红书 App 扫码",
                    "channels": CHANNELS, "import_dir": _vendor_dir(),
                    "counts": {"notes": 0, "comments": 0}}
        records = [r for r in (cache.get("comments") or []) if isinstance(r, dict)]
        cache_at = cache.get("at")
    elif mode == "vendor":
        records = list(_iter_comments())
        cache_at = None
    else:
        return {"status": "unavailable", "mode": mode, "samples": 0,
                "reason": f"小红书通道未开启（{mode}）",
                "hint": "可在卡片里切到「本机扫码」，或购买千瓜/新榜导出 JSON 走 vendor",
                "channels": CHANNELS, "import_dir": _vendor_dir(),
                "counts": {"notes": 0, "comments": 0}}
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    words = _panel_words()
    want = (panel or "zhipu").lower()
    recs, seen = [], set()
    panel_counts = {p: 0 for p in PANEL_LABEL}
    for r in records:
        blob = f"{r.get('note_title','')} {r.get('text','')}".lower()
        hit_panel = r.get("panel") or panel_for_text(blob, words)
        if hit_panel in panel_counts:
            panel_counts[hit_panel] += 1
        if want != "all" and hit_panel and hit_panel != want:
            continue
        if want != "all" and not hit_panel:
            continue
        if q and q.lower() not in blob:
            continue
        d = _day(r.get("t")) or r.get("day") or ""
        if d and d < cutoff:
            continue
        key = (r["note_id"], r["text"], r["t"])
        if key in seen:
            continue
        seen.add(key)
        r["day"] = d or datetime.now().strftime("%Y-%m-%d")
        r["panel"] = hit_panel
        recs.append(r)
    if not recs:
        label = PANEL_LABEL.get(want, want)
        hint = (f"导出时勾选评论字段（comments[]），并确认笔记标题/正文含「{'/'.join(words.get(want, ()) [:4])}」"
                if want != "all" else "导出时勾选评论字段（comments[]）")
        return {"status": "zero_hit", "mode": mode, "samples": 0, "panel": want,
                "reason": f"数据里没有{label}相关评论",
                "hint": hint, "keywords": keywords(), "panels": panel_counts,
                "channels": CHANNELS, "import_dir": _vendor_dir(),
                "counts": {"notes": 0, "comments": 0}}
    pol = {"pos": 0, "neu": 0, "neg": 0}
    topics = {}
    for r in recs:
        text = r["text"]
        ps = sum(w in text for w in _POS_WORDS)
        ng = sum(w in text for w in _NEG_WORDS)
        p = "pos" if ps > ng else "neg" if ng > ps else "neu"
        pol[p] += 1
        for w in _TOPIC_WORDS:
            if w in r["text"]:
                topics[w] = topics.get(w, 0) + 1
    div = round(min(pol["pos"], pol["neg"]) / max(1, pol["pos"] + pol["neg"]), 2)
    day_list = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(days - 1, -1, -1)]
    idx = {d: i for i, d in enumerate(day_list)}
    series = [0] * len(day_list)
    for r in recs:
        i = idx.get(r["day"])
        if i is not None:
            series[i] += 1
    recs.sort(key=lambda x: (x["likes"], x["day"]), reverse=True)
    return {
        "status": "ok", "mode": mode, "days": day_list,
        "panel": want, "panels": panel_counts, "keywords": keywords(),
        "channels": CHANNELS, "import_dir": _vendor_dir(),
        "cache_at": cache_at,
        "counts": {"notes": len({r["note_id"] for r in recs if r["note_id"]}),
                   "comments": len(recs), "likes": sum(r["likes"] for r in recs)},
        "polarity": dict(pol, note="评论词典判定，只作趋势参考"),
        "divergence": {"value": div, "label": "0=一边倒，0.5=对半"},
        "topics": [{"word": k, "n": v} for k, v in
                   sorted(topics.items(), key=lambda kv: -kv[1])[:8]],
        "series": series,
        "samples": recs[:12],
    }


def fetch_vendor(limit: int = 200) -> list[dict]:
    """把 vendor 数据转成标准 entry，供 pipeline 落库"""
    out = []
    for it in _vendor_rows()[:limit]:
        title = it.get("title") or it.get("desc") or ""
        if not title:
            continue
        out.append({
            "title": f"[小红书] {title[:80]}",
            "link": it.get("url") or it.get("note_url") or "",
            "desc": (it.get("desc") or it.get("content") or "")[:300],
            "guid": f"xhs-{it.get('note_id') or abs(hash(title)) % (10**12)}",
            "t_valid": (it.get("time") or it.get("publish_time") or
                        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
            "_body": it.get("desc") or it.get("content") or "",
        })
    return out
