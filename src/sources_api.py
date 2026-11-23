"""JSON API 型信源：券商研报（东财）+ 上市公司公告（巨潮）。

和 RSS 源的区别：这两个给的是**结构化事实**（谁、什么机构、哪天、什么 PDF），
比媒体报道更接近产业链一手信息。

实测（2026-11-23）：
  东财研报  https://reportapi.eastmoney.com/report/list      200 / 1137 hits
  巨潮公告  http://www.cninfo.com.cn/new/hisAnnouncement/query 200 / 2564 条
"""
import json
import re

import ratelimit
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import wechat_guard

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# 只要和 AI 产业链沾边的研报/公告才收（否则一天上千条全是消费/医药）
AI_KEYWORDS = [
    "人工智能", "AI", "大模型", "算力", "芯片", "半导体", "GPU", "服务器", "数据中心",
    "机器人", "具身", "智能驾驶", "自动驾驶", "智算", "推理", "训练", "模型", "光模块",
    "液冷", "先进封装", "存储", "智谱", "昇腾", "英伟达", "国产替代", "算法",
]

# 交易/资源流向词：只有「AI 词 + 交易词」同时命中才收，避免快讯流全是宏观噪声
DEAL_KEYWORDS = [
    "融资", "投资", "收购", "并购", "增资", "注资", "出资", "领投", "参投",
    "战略投资", "入股", "控股", "股权转让", "要约", "合并", "募资", "估值",
    "重大合同", "合同", "订单", "中标", "采购", "租用", "租赁", "代工",
    "产能", "智算中心", "数据中心", "IPO", "上市",
]

# 拉丁关键词必须按词边界匹配，否则 "AI" 会命中 said / chain / capital
_AI_RE = re.compile(
    r"人工智能|大模型|算力|芯片|半导体|服务器|数据中心|机器人|具身|智能驾驶|"
    r"自动驾驶|智算|推理|训练|光模块|液冷|先进封装|存储|智谱|昇腾|英伟达|"
    r"国产替代|算法|OpenAI|Anthropic|DeepSeek|NVIDIA|CoreWeave|Mistral|"
    r"Perplexity|Cohere|Databricks|Palantir|Broadcom|TSMC|Micron|"
    r"月之暗面|MiniMax|百川智能|零一万物|阶跃星辰|商汤|科大讯飞|宇树|智元|"
    r"银河通用|寒武纪|海光信息|摩尔线程|壁仞|燧原|沐曦|阿里云|腾讯云|华为云|"
    r"火山引擎|百度智能云|甲骨文|微软|谷歌|Mistral|xAI|"
    r"(?-i:Arm)(?![A-Za-z])|(?-i:AMD)(?![A-Za-z])|(?-i:IBM)(?![A-Za-z])|"
    r"(?<![A-Za-z0-9])AI(?![A-Za-z0-9])|(?<![A-Za-z0-9])AGI(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])LLM(?![A-Za-z0-9])|(?<![A-Za-z0-9])GPU(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])HBM(?![A-Za-z0-9])|(?<![A-Za-z0-9])TPU(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])NPU(?![A-Za-z0-9])|(?<![A-Za-z0-9])ASIC(?![A-Za-z0-9])|"
    r"artificial intelligence|machine learning|generative ai|"
    r"data cent(?:er|re)s?|semiconductors?|AI infrastructure",
    re.I)

# 「强交易」词：已知 AI 公司发的公告用这一档，避免「投资者关系」「控股股东」噪声
_STRONG_DEAL_RE = re.compile(
    r"收购|并购|增资|注资|出资|中标|重大合同|合同|订单|采购|租用|租赁|代工|"
    r"股权转让|对外投资|战略投资|投资协议|算力服务|算力租赁|算力技术|智算|"
    r"数据中心|服务器|设备采购|(?<![A-Za-z0-9])IPO(?![A-Za-z0-9])|募资"
)

_DEAL_RE = re.compile(
    r"融资|投资|收购|并购|增资|注资|出资|领投|参投|战略投资|入股|控股|"
    r"股权转让|要约|合并|募资|估值|重大合同|合同|订单|中标|采购|租用|租赁|"
    r"代工|产能|(?<![A-Za-z0-9])IPO(?![A-Za-z0-9])|上市|"
    r"(?<![A-Za-z])acqui(?:re|res|red|ring|sition|sitions)(?![A-Za-z])|"
    r"(?<![A-Za-z])raise[sd]?(?![A-Za-z])|(?<![A-Za-z])funding(?![A-Za-z])|"
    r"(?<![A-Za-z])invest(?:s|ed|ing|ment|ments)?(?![A-Za-z])|"
    r"(?<![A-Za-z])merger(?![A-Za-z])|(?<![A-Za-z])deal(?![A-Za-z])|"
    r"(?<![A-Za-z])contracts?(?![A-Za-z])|(?<![A-Za-z])orders?(?![A-Za-z])|"
    r"(?<![A-Za-z])procurement(?![A-Za-z])|(?<![A-Za-z])acquihire(?![A-Za-z])|"
    r"(?<![A-Za-z])buy(?:s|ing)?(?![A-Za-z])",
    re.I)


def _get(url: str, timeout=15, accept="application/json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with ratelimit.IO_GATE:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


def _post(url: str, data: dict, timeout=15) -> bytes:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"})
    with ratelimit.IO_GATE:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


def _hit(text: str) -> bool:
    """AI 产业链相关（拉丁词按词边界，避免 'AI' 命中 said/chain）"""
    return bool(_AI_RE.search(text or ""))


def _deal_hit(text: str) -> bool:
    return bool(_DEAL_RE.search(text or ""))


def _strong_deal_hit(text: str) -> bool:
    return bool(_STRONG_DEAL_RE.search(text or ""))


def focus_text(text: str) -> bool:
    """交易 + (AI 关键词 或 已知 AI 主体) 才算有效信号（供快讯/资讯流过滤）"""
    t = text or ""
    return bool(_DEAL_RE.search(t)) and bool(_AI_RE.search(t) or _known_ai(t))


_AI_NAMES = None
# 这些是「市场」而不是公司，不能用来认领公告
_KNOWN_SKIP = {"资本市场", "一级市场", "二级市场", "A股", "港股", "美股"}


def _ai_names() -> set[str]:
    """AI 产业链已知主体名（取自 flows 词表），用于公告「按公司认领」"""
    global _AI_NAMES
    if _AI_NAMES is None:
        try:
            import flows
            names = set()
            for canon, _layer, aliases in flows.ENTITY_SPEC:
                for a in [canon] + (aliases or "").split("|"):
                    a = (a or "").strip()
                    if (len(a) >= 2 and re.search(r"[\u4e00-\u9fff]", a)
                            and a not in _KNOWN_SKIP):
                        names.add(a)
            _AI_NAMES = names
        except Exception:
            _AI_NAMES = set()
    return _AI_NAMES


def _known_ai(text: str) -> bool:
    return any(n in (text or "") for n in _ai_names())


def _cn_time(s: str) -> str:
    """北京时间字符串 → UTC ISO（'2026-11-23 01:38:39' / '2026-11-23 00:00:00'）"""
    try:
        dt = datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(s: str, limit: int = 60000) -> str:
    from collector import html_to_text
    return html_to_text(s or "", limit)


def _body_clean(s: str, title: str = "", limit: int = 60000) -> str:
    from collector import clean_body
    return clean_body(_plain(s, limit), title)[:limit]


# ── 券商研报 ──
def fetch_reports(days: int = 3, page_size: int = 100) -> list[dict]:
    end = datetime.now()
    begin = end - timedelta(days=days)
    url = ("https://reportapi.eastmoney.com/report/list?"
           + urllib.parse.urlencode({
               "cb": "", "industryCode": "*", "pageSize": page_size, "industry": "*",
               "rating": "", "ratingChange": "", "beginTime": begin.strftime("%Y-%m-%d"),
               "endTime": end.strftime("%Y-%m-%d"), "pageNo": 1, "fields": "", "qType": 0,
               "orgCode": "", "code": "*", "rcode": "", "p": 1, "pageNum": 1}))
    raw = _get(url)
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    out = []
    for it in data.get("data") or []:
        title = it.get("title") or ""
        name = it.get("stockName") or ""
        if not _hit(title + name):
            continue
        code = it.get("infoCode") or ""
        out.append({
            "title": f"[研报] {it.get('orgSName','')}：{title}" + (f"（{name}）" if name else ""),
            "link": f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={code}",
            "desc": f"{it.get('orgSName','')} · {name} {it.get('stockCode','')}",
            "guid": f"em-report-{code}",
            "t_valid": (it.get("publishDate") or "")[:19].replace(" ", "T") + "Z",
        })
    return out


# ── 上市公司公告（按关键词搜索，不是全量过滤）──
NOTICE_KEYS = ["算力", "智算", "芯片", "大模型", "机器人", "中标", "GPU",
               "服务器", "数据中心", "液冷", "光模块", "算力服务", "算力租赁",
               "智算中心", "AI服务器"]


def _clean_hl(s: str) -> str:
    """去掉接口返回的高亮标签 <em>"""
    import re
    return re.sub(r"</?em>", "", s or "")


def fetch_announcements(days: int = 3, page_size: int = 30) -> list[dict]:
    end = datetime.now()
    begin = end - timedelta(days=days)
    seen, out = set(), []
    for kw in NOTICE_KEYS:
        data = {
            "pageNum": 1, "pageSize": page_size, "column": "szse", "tabName": "fulltext",
            "plate": "", "stock": "", "searchkey": kw, "secid": "", "category": "",
            "trade": "", "seDate": f"{begin:%Y-%m-%d}~{end:%Y-%m-%d}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        }
        try:
            raw = _post("http://www.cninfo.com.cn/new/hisAnnouncement/query", data)
            j = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            continue
        for it in (j.get("announcements") or []):
            aid = it.get("announcementId")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            title = _clean_hl(it.get("announcementTitle") or "")
            name = _clean_hl(it.get("secName") or "")
            adj = it.get("adjunctUrl") or ""
            ts = it.get("announcementTime") or 0
            try:
                tv = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                tv = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append({
                "title": f"[公告] {name}：{title}",
                "link": f"http://static.cninfo.com.cn/{adj}",
                "desc": f"{name} {it.get('secCode','')} · 命中「{kw}」",
                "guid": f"cninfo-{aid}",
                "t_valid": tv,
            })
        time.sleep(0.4)   # 别把人家接口打挂
    return out


# ── SEC EDGAR：美股 AI 产业链公司的一手披露（免费公开，需带 UA）──
# 全市场泛搜会捞出一堆无关小公司（实测 "artificial intelligence" 命中 10000+，
# 前排全是 ECOMINAS / Valor Energy 这种），所以走**关注清单 + CIK 定向**。
SEC_UA = "LLMIntelAgent/1.0 (research; contact: ops@example.com)"

SEC_WATCH = {
    "0001045810": "英伟达 NVDA",
    "0000002488": "AMD",
    "0000789019": "微软 MSFT",
    "0001652044": "谷歌 GOOGL",
    "0001326801": "Meta",
    "0001018724": "亚马逊 AMZN",
    "0001730168": "博通 AVGO",
    "0001046179": "台积电 TSM",
    "0000723125": "美光 MU",
    "0001375365": "超微 SMCI",
    "0001674101": "Vertiv VRT",
    "0001321655": "Palantir PLTR",
    "0000937966": "阿斯麦 ASML",
    "0001973239": "Arm",
}
# 只要"有事件含义"的表单，Form 4（内部人交易）噪声太大，先不要。
# 8-K/6-K/SC 13D 交给 fetch_sec_deals（带正文），这里避免重复入库。
SEC_KEEP = {"10-Q", "10-K", "S-1", "S-1/A", "13F-HR",
            "SC 13G", "DEF 14A", "20-F"}


def _sec_get(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": SEC_UA, "Accept": "application/json, text/xml, */*",
        "Accept-Encoding": "gzip, deflate"})
    with ratelimit.IO_GATE:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return raw


def fetch_sec_filings(days: int = 30, limit_per_cik: int = 8) -> list[dict]:
    """按关注清单拉美股公司披露：10-Q（财报）/ 10-K / S-1（IPO）/ 13F 等。

    这是**一手事实**，不是投行观点。投行研报（GS/MS/BlackRock）是付费客户专享，不抓。
    走 data.sec.gov/submissions（比 FTS 的 q="" 稳定，实测 NVDA 返回 1000+ 条索引）。
    """
    end = datetime.now()
    begin = end - timedelta(days=days)
    startdt = begin.strftime("%Y-%m-%d")
    out, seen = [], set()
    for cik, label in SEC_WATCH.items():
        try:
            j = json.loads(_sec_get(
                f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=25
            ).decode("utf-8", "ignore"))
            r = (j.get("filings") or {}).get("recent") or {}
            forms = r.get("form") or []
            dates = r.get("filingDate") or []
            accs = r.get("accessionNumber") or []
            docs = r.get("primaryDocument") or []
        except Exception:
            time.sleep(0.25)
            continue
        n = 0
        for i, form in enumerate(forms):
            day = dates[i] if i < len(dates) else ""
            if not day or day < startdt:
                break
            if form not in SEC_KEEP:
                continue
            acc = accs[i] if i < len(accs) else ""
            doc = docs[i] if i < len(docs) else ""
            if not acc or not doc:
                continue
            fid = f"{acc}:{doc}"
            if fid in seen:
                continue
            seen.add(fid)
            out.append({
                "title": f"[SEC {form}] {label}",
                "link": _sec_link(cik, acc, doc),
                "desc": f"{label} · {day} · {form}",
                "guid": f"sec-{fid}",
                "t_valid": day + "T00:00:00Z",
            })
            n += 1
            if n >= limit_per_cik:
                break
        time.sleep(0.25)   # SEC 要求 ≤10 req/s
    out.sort(key=lambda x: x["t_valid"], reverse=True)
    return out


# ── 贝莱德中国站「投资观点」：全球投资周报 + 全球投资展望（公开发布，非付费研报）──
BLK_WEEKLY = "https://www.blackrock.com/cn/global-weekly-commentary"
BLK_OUTLOOK = "https://www.blackrock.com/cn/insights/global-investment-outlook"
_NAV_H2 = {"长期投资策略", "颠覆性趋势的交汇", "战术配置细分观点", "关注“转型机会”",
           "从驱动因素到投资组合表达", "市场洞察", "相关内容", "重要信息"}


def _fetch_html(url: str, timeout=25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
        "Accept-Language": "zh-CN,zh;q=0.9"})
    with ratelimit.IO_GATE:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")


def _blk_parse(html: str, url: str) -> dict | None:
    """从贝莱德中文页解析出：发布日期 + 标题 + 正文"""
    import re
    import html as _h

    def txt(s):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()

    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", html)
    date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
    title = ""
    for tag in ("h1", "h2"):
        for mm in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.S):
            cand = txt(mm.group(1))
            if len(cand) < 8 or cand in _NAV_H2:
                continue
            if "点击" in cand or "链接" in cand or "QDII" in cand:
                continue
            if tag == "h1" and ("投资评论" in cand or "展望" in cand or "市场" in cand):
                continue        # 栏目名不是标题
            title = cand
            break
        if title:
            break
    if not title:
        mo = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        title = _h.unescape(mo.group(1)) if mo else ""
    if not title or not date:
        return None
    # 正文：取主要 content 区，退化为整页文本
    body = ""
    mc = re.search(r'<div[^>]+class="[^"]*(?:article-body|content-body|rich-text|article__body)[^"]*"[^>]*>(.*?)</div>\s*</div>',
                   html, re.S)
    from collector import html_to_text, clean_body
    body = html_to_text(mc.group(1) if mc else html, 20000)
    body = clean_body(body, title)
    # 贝莱德是客户端渲染，容器不固定。实测正文前面依次是：
    #   站点导航 → 标题 → 视频脚本（To view this video… / Opening frame… / SCRIPT）
    # 所以先按标题定位，再按「中文字符占比」找到第一段实质中文，从那里开始。
    pos = body.find(title[:12])
    if pos >= 0:
        body = body[pos + len(title):]

    def _cjk_ratio(s: str) -> float:
        c = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        return c / max(1, len(s))

    lines = [l for l in body.split("\n") if l.strip()]
    start = 0
    for i, l in enumerate(lines):
        if len(l) >= 40 and _cjk_ratio(l) >= 0.5:
            start = i
            break
    body = "\n".join([title] + lines[start:])
    # 尾部站点区块（作者卡片/免责/跳转提示）
    for tail in ("本文仅供参考", "重要信息披露", "投资涉及风险", "© ", "Copyright"):
        j = body.find(tail)
        if j > 200:
            body = body[:j]
            break
    return {"title": title, "date": date, "body": body, "url": url}


def fetch_blackrock_cn() -> list[dict]:
    """贝莱德中文站：每周投资评论（1 篇/周）+ 全球投资展望（PDF）。

    这是**公开发布的市场观点**，不是付费研报——投行的完整研报仍然是客户专享，不抓。
    周报是同一 URL 每周更新，所以用「标题+日期」做 guid 去重。
    """
    out = []
    try:
        wk = _blk_parse(_fetch_html(BLK_WEEKLY), BLK_WEEKLY)
        if wk:
            out.append({
                "title": f"[贝莱德周报] {wk['title']}",
                "link": wk["url"],
                "desc": wk["body"][:300],
                "guid": f"blk-weekly-{wk['date']}-{wk['title'][:16]}",
                "t_valid": wk["date"] + "T00:00:00Z",
                "_body": wk["body"],
            })
    except Exception:
        pass
    try:
        h = _fetch_html(BLK_OUTLOOK)
        import re
        for mm in re.finditer(r'href="(/cn/literature/market-commentary/[^"]+)"[^>]*>(.*?)</a>', h, re.S):
            href = mm.group(1)
            label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", mm.group(2))).strip()
            if not label or len(label) < 4:
                continue
            # 链接文字是「阅读贝莱德的2026年第四季度投资展望(PDF) 下载贝莱德《…》报告」这种拼接，
            # 提取《…》里的正式名；没有书名号就用年份+季度
            mt = re.search(r"《([^》]{2,40})》", label)
            if mt:
                label = mt.group(1)
            else:
                mq = re.search(r"(20\d{2})\s*年\s*(第[一二三四1-4]季度|年中|上半年|下半年)?", label)
                label = (mq.group(0) + "投资展望") if mq else re.sub(r"^阅读|^下载", "", label)[:40]
            m2 = re.search(r"(20\d{2})", label + href)
            yr = m2.group(1) if m2 else ""
            out.append({
                "title": f"[贝莱德展望] {label[:70]}",
                "link": "https://www.blackrock.com" + href,
                "desc": f"贝莱德智库投资展望 · {yr}",
                "guid": f"blk-outlook-{href[-40:]}",
                "t_valid": f"{yr}-01-01T00:00:00Z" if yr else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    except Exception:
        pass
    return out


# ── 高盛 Insights：简短投资观点（sitemap 发现 + og 摘要）──
GS_SITEMAP = "https://www.goldmansachs.com/sitemap-1.xml"
GS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
# 只要文章页，避开 robots 里 Disallow 的 /insights/top-of-mind/
GS_PATH_PREFIX = ("https://www.goldmansachs.com/insights/articles/",
                  "https://www.goldmansachs.com/insights/the-markets/")


def fetch_goldman_insights(days: int = 14, limit: int = 12) -> list[dict]:
    """高盛公开 Insights 短文：标题 + og:description（一两句结论）+ 正文首段。

    文章列表是客户端渲染，所以走 sitemap 发现（带 lastmod），再逐篇取 og 摘要。
    这是**公开发布的观点短文**，不是付费研报。
    """
    import re
    import html as _h
    end = datetime.now()
    begin = end - timedelta(days=days)
    try:
        xml = _fetch_html(GS_SITEMAP, timeout=40)
    except Exception:
        return []
    cand = []
    for m in re.finditer(r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?", xml):
        u, d = m.group(1), (m.group(2) or "")
        if not u.startswith(GS_PATH_PREFIX):
            continue
        day = d[:10]
        if not day or day < begin.strftime("%Y-%m-%d"):
            continue
        cand.append((day, u))
    cand.sort(reverse=True)
    out = []
    for day, u in cand[:limit]:
        try:
            h = _fetch_html(u, timeout=25)
        except Exception:
            continue
        def og(prop):
            mm = re.search(rf'property="og:{prop}" content="([^"]*)"', h)
            return _h.unescape(mm.group(1)).strip() if mm else ""
        title = og("title")
        desc = og("description")
        if not title:
            continue
        pub = ""
        mp = re.search(r'"datePublished":"(\d{4}-\d{2}-\d{2})', h)
        pub = mp.group(1) if mp else day
        # 正文：跳过导航段落，取前若干段
        paras = []
        for pm in re.finditer(r"<p[^>]*>(.{80,}?)</p>", h, re.S):
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", pm.group(1))).strip()
            if "Learn more about" in txt or "Explore" in txt or "Follow Us" in txt:
                continue
            paras.append(txt)
            if len(paras) >= 8:
                break
        body = "\n".join([title, desc] + paras[:6])
        out.append({
            "title": f"[高盛观点] {title[:80]}",
            "link": u,
            "desc": desc[:300],
            "guid": f"gs-{u.rsplit('/', 1)[-1][:60]}",
            "t_valid": pub + "T00:00:00Z",
            "_body": body,
        })
        time.sleep(0.4)
    return out


# ── 公众号正文与阅读数 ──
def fetch_wechat_article(url: str) -> dict:
    """抓 mp.weixin.qq.com 文章：正文 + 阅读数/点赞（页面 JS 变量里）。

    返回 {ok, title, author, publish_at, content, read_num, like_num, error}
    注意：阅读数是页面首次渲染时的快照，不是实时值。
    """
    import re
    import html as _html
    try:
        raw = _get(url, timeout=20).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise wechat_guard.WechatRateLimited(f"HTTP 429：{url}")
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    if wechat_guard.looks_limited(raw):
        raise wechat_guard.WechatRateLimited("页面提示访问过于频繁")

    def find(pat, default=None):
        m = re.search(pat, raw)
        return _html.unescape(m.group(1)) if m else default

    title = find(r'var\s+msg_title\s*=\s*"([^"]*)"') or find(r'<meta property="og:title" content="([^"]*)"')
    account = find(r'id="js_name">\s*([^<]*)<')
    author = account or find(r'var\s+nickname\s*=\s*"([^"]*)"') or find(r'var\s+author\s*=\s*"([^"]*)"')
    ct = find(r'var\s+ct\s*=\s*"?(\d{10})')
    read_num = find(r"var\s+read_num\s*=\s*(\d+)")
    like_num = find(r"var\s+like_num\s*=\s*(\d+)")
    body = ""
    m = re.search(r'<div class="rich_media_content[^"]*"[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script',
                  raw, re.S)
    if not m:
        m = re.search(r'id="js_content"[^>]*>(.*?)</div>', raw, re.S)
    if m:
        from collector import html_to_text
        body = html_to_text(m.group(1), 60000)
    publish_at = ""
    if ct:
        try:
            publish_at = datetime.utcfromtimestamp(int(ct)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return {"ok": bool(title and body), "title": title, "author": author,
            "account": account or "",
            "publish_at": publish_at, "content": body,
            "read_num": int(read_num) if read_num else None,
            "like_num": int(like_num) if like_num else None,
            # 实测 2026-11-23：现代公众号页面**不在 HTML 里输出阅读数**（客户端另行拉取），
            # 所以这里如实标 unavailable，不编造数字。
            "read_num_source": "html_var" if read_num else "unavailable",
            "error": None if (title and body) else "未解析到标题或正文（可能需要登录或被限制）"}


def fetch_wechat_urls(urls: list[str], limit: int = 20) -> list[dict]:
    """公众号 URL 直抓通道：给一批文章链接，返回标准 entry 列表（供 pipeline 消费）"""
    out = []
    for u in urls[:limit]:
        u = (u or "").strip()
        if "mp.weixin.qq.com" not in u:
            continue
        try:
            r = fetch_wechat_article(u)
        except wechat_guard.WechatRateLimited as e:
            # 已成功的文章带回去，剩余的不再尝试
            raise wechat_guard.WechatRateLimited(str(e), fetched=len(out), entries=out)
        if not r["ok"]:
            continue
        out.append({
            "title": r["title"],
            "link": u,
            "_source_name": r.get("account") or "",
            "desc": (r["content"] or "")[:300],
            "guid": f"wx-{abs(hash(u)) % (10**12)}",
            "t_valid": r["publish_at"] or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "_body": r["content"],
            "_read_num": r["read_num"],
            "_like_num": r["like_num"],
        })
        time.sleep(0.8)
    return out


# ══════════════════════════════════════════════════════════════════════
# 免费交易流数据源（2026-11-23 实测可用）
#   新浪 7x24      https://zhibo.sina.com.cn/api/zhibo/feed
#   东财快讯       https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_...
#   东财公告+全文  https://np-anotice-stock.eastmoney.com/api/security/ann
#                  https://np-cnotice-stock.eastmoney.com/api/content/ann
#   SEC 交易披露   https://efts.sec.gov/LATEST/search-index + data.sec.gov/submissions
# 这些源都**带全文**，让 flows.py 能在正文里抽 (from,to,channel,amount)。
# ══════════════════════════════════════════════════════════════════════

def fetch_sina_7x24(pages: int = 2, page_size: int = 50) -> list[dict]:
    """新浪财经 7x24 快讯：全网最快的财经短讯，含大量融资/订单/收购快报。"""
    out, seen = [], set()
    for p in range(1, max(1, pages) + 1):
        url = ("https://zhibo.sina.com.cn/api/zhibo/feed?"
               + urllib.parse.urlencode({"page": p, "page_size": page_size, "zhibo_id": 152}))
        try:
            j = json.loads(_get(url, timeout=20).decode("utf-8", "ignore"))
            lst = ((((j.get("result") or {}).get("data") or {}).get("feed") or {})
                   .get("list") or [])
        except Exception:
            break
        if not lst:
            break
        for it in lst:
            text = _body_clean(it.get("rich_text") or "", "", 20000)
            if not focus_text(text):
                continue
            sid = str(it.get("id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            title = re.sub(r"\s+", " ", text)[:90]
            out.append({
                "title": f"[新浪7x24] {title}",
                "link": it.get("docurl") or f"https://finance.sina.com.cn/7x24/?id={sid}",
                "desc": text[:300],
                "guid": f"sina7x24-{sid}",
                "t_valid": _cn_time(it.get("create_time") or ""),
                "_body": text,
            })
        time.sleep(0.2)
    return out


def fetch_em_kuaixun(pages: int = 2, page_size: int = 50) -> list[dict]:
    """东方财富 7x24 快讯：与新浪互补（有时先发）。"""
    out, seen = [], set()
    for p in range(1, max(1, pages) + 1):
        url = (f"https://newsapi.eastmoney.com/kuaixun/v1/"
               f"getlist_102_ajaxResult_{page_size}_{p}_.html")
        try:
            raw = _get(url, timeout=20).decode("utf-8", "ignore")
            i, k = raw.find("{"), raw.rfind("}")
            j = json.loads(raw[i:k + 1]) if i >= 0 and k > i else {}
        except Exception:
            break
        lst = j.get("LivesList") or []
        if not lst:
            break
        for it in lst:
            title = it.get("title") or ""
            digest = it.get("digest") or ""
            text = _body_clean(f"{title}\n{digest}", title, 20000)
            if not focus_text(text):
                continue
            nid = str(it.get("newsid") or "")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            out.append({
                "title": f"[东财快讯] {title[:90]}",
                "link": it.get("url_w") or f"https://finance.eastmoney.com/a/{nid}.html",
                "desc": digest[:300],
                "guid": f"em-kx-{nid}",
                "t_valid": _cn_time(it.get("showtime") or ""),
                "_body": text,
            })
        time.sleep(0.2)
    return out


def _epoch_iso(ts) -> str:
    """秒级时间戳 → UTC ISO"""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_ths_7x24(pages: int = 3, page_size: int = 100) -> list[dict]:
    """同花顺 7x24 快讯：A 股公告式快报多，增资/中标/收购的首发常在这里。"""
    out, seen = [], set()
    for p in range(1, max(1, pages) + 1):
        url = ("https://news.10jqka.com.cn/tapp/news/push/stock/?"
               + urllib.parse.urlencode({"page": p, "tag": "", "track": "website",
                                         "pagesize": page_size}))
        try:
            j = json.loads(_get(url, timeout=20).decode("utf-8", "ignore"))
            lst = (j.get("data") or {}).get("list") or []
        except Exception:
            break
        if not lst:
            break
        for it in lst:
            title = it.get("title") or ""
            digest = it.get("digest") or ""
            text = _body_clean(f"{title}\n{digest}", title, 20000)
            if not focus_text(text):
                continue
            nid = str(it.get("id") or "")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            out.append({
                "title": f"[同花顺7x24] {title[:90]}",
                "link": it.get("url") or "https://news.10jqka.com.cn/",
                "desc": digest[:300],
                "guid": f"ths7x24-{nid}",
                "t_valid": _epoch_iso(it.get("ctime")),
                "_body": text,
            })
        time.sleep(0.2)
    return out


def fetch_wscn_lives(pages: int = 2, limit: int = 50) -> list[dict]:
    """华尔街见闻全球快讯：海外融资/IPO 首发快。"""
    out, seen, cursor = [], set(), ""
    for _ in range(max(1, pages)):
        url = ("https://api-one-wscn.awtmt.com/apiv1/content/lives?"
               + urllib.parse.urlencode({"channel": "global-channel", "client": "pc",
                                         "limit": limit, "cursor": cursor}))
        try:
            j = json.loads(_get(url, timeout=20).decode("utf-8", "ignore"))
            d = j.get("data") or {}
            lst = d.get("items") or []
        except Exception:
            break
        if not lst:
            break
        for it in lst:
            raw_title = it.get("title") or ""
            text = _body_clean((it.get("content_text") or "") + " " + raw_title,
                               raw_title, 20000)
            if not focus_text(text):
                continue
            sid = str(it.get("id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            title = re.sub(r"\s+", " ", raw_title or text)[:90]
            out.append({
                "title": f"[见闻快讯] {title}",
                "link": it.get("uri") or f"https://wallstreetcn.com/livenews/{sid}",
                "desc": text[:300],
                "guid": f"wscn-{sid}",
                "t_valid": _epoch_iso(it.get("display_time")),
                "_body": text,
            })
        cursor = str(d.get("next_cursor") or "")
        if not cursor:
            break
        time.sleep(0.2)
    return out


def _em_notice_body(art_code: str) -> str:
    """东财公告正文接口：返回公告全文（不是 PDF，直接可读文本）"""
    if not art_code:
        return ""
    url = ("https://np-cnotice-stock.eastmoney.com/api/content/ann?"
           + urllib.parse.urlencode({"art_code": art_code, "client_source": "web",
                                     "page_index": 1}))
    try:
        j = json.loads(_get(url, timeout=20).decode("utf-8", "ignore"))
        d = j.get("data") or {}
        return _body_clean(d.get("notice_content") or "", d.get("notice_title") or "", 40000)
    except Exception:
        return ""


def fetch_em_announcements(pages: int = 5, page_size: int = 100,
                           max_body: int = 60) -> list[dict]:
    """东财 A 股公告：AI 公司 × 交易关键词 → 拉全文。

    公告是一手事实（签约主体、金额、对手方都在正文），比媒体转述更可靠。
    只认两类：
      ① 标题里有 AI 关键词 + 交易关键词；
      ② 已知 AI 产业链上市公司发的交易类公告（标题可能只写「关于收购…」）。
    """
    out, seen = [], set()
    bodies = 0
    for page in range(1, max(1, pages) + 1):
        url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?"
               + urllib.parse.urlencode({
                   "sr": -1, "page_size": page_size, "page_index": page,
                   "ann_type": "A", "client_source": "web", "f_node": 0}))
        try:
            j = json.loads(_get(url, timeout=20).decode("utf-8", "ignore"))
            lst = ((j.get("data") or {}).get("list") or [])
        except Exception:
            break
        if not lst:
            break
        for it in lst:
            raw_title = it.get("title") or ""
            codes = it.get("codes") or [{}]
            name = (codes[0] or {}).get("short_name") or ""
            stock_code = (codes[0] or {}).get("stock_code") or ""
            if not raw_title:
                continue
            # ① AI 关键词标题 + 任意交易词；② 已知 AI 公司 + 强交易词
            ai_title = _hit(raw_title)
            known = _known_ai(name)
            if not ((ai_title and _deal_hit(raw_title)) or
                    (known and _strong_deal_hit(raw_title))):
                continue
            aid = it.get("art_code") or ""
            if not aid or aid in seen:
                continue
            seen.add(aid)
            body = ""
            if bodies < max_body:
                body = _em_notice_body(aid)
                bodies += 1
                time.sleep(0.15)
            title = raw_title
            if name and title.startswith(name):
                title = title[len(name):].lstrip(":： ")
            full = f"{name} {title} {body}"
            if not ((ai_title and focus_text(full)) or
                    (known and _strong_deal_hit(full))):
                continue
            out.append({
                "title": f"[公告] {name}：{title}",
                "link": (f"https://data.eastmoney.com/notices/detail/"
                         f"{stock_code}/{aid}.html"),
                "desc": (body or title)[:300],
                "guid": f"em-ann-{aid}",
                "t_valid": _cn_time(it.get("notice_date") or ""),
                "_body": body or title,
            })
        time.sleep(0.2)
    out.sort(key=lambda x: x["t_valid"], reverse=True)
    return out


# SEC 全文检索：q 里同时给 AI 词和交易词，EDGAR 默认 AND
SEC_DEAL_QUERIES = [
    ('"artificial intelligence" acquisition', "8-K"),
    ('"artificial intelligence" "strategic investment"', "8-K"),
    ('"artificial intelligence" acquire', "8-K"),
    ('"AI infrastructure" acquisition', "8-K"),
    ('"data center" acquisition', "8-K"),
    ('"GPU" supply agreement', "8-K"),
    ('"artificial intelligence" investment', "6-K"),
]


def _sec_link(cik: str, adsh: str, filename: str) -> str:
    try:
        c = str(int(cik))
    except Exception:
        c = cik
    return (f"https://www.sec.gov/Archives/edgar/data/{c}/"
            f"{(adsh or '').replace('-', '')}/{filename}")


def _sec_name(s: dict) -> str:
    names = s.get("display_names") or []
    n = names[0] if names else "SEC filing"
    return re.sub(r"\s*\(CIK \d+\)", "", n).strip()


def _sec_score(file_type: str) -> int:
    ft = (file_type or "").lower()
    if ft.startswith("ex-") and ft.endswith(".htm"):
        return 4
    if ft.endswith(".htm"):
        return 3
    if ft.endswith(".txt"):
        return 2
    if ft.endswith(".xml"):
        return 0
    return 1


def _sec_body(link: str) -> str:
    try:
        raw = _sec_get(link, timeout=25)
    except Exception:
        return ""
    return _body_clean(raw.decode("utf-8", "ignore"), "", 60000)


def fetch_sec_deals(days: int = 10, per_query: int = 5, per_cik: int = 2) -> list[dict]:
    """美股 AI 产业链的交易披露（8-K / 6-K 全文 + 关注清单）。

    两条腿：
      ① EDGAR 全文检索「AI + 收购/投资/供货协议」，捞中小公司；
      ② 关注清单（英伟达/AMD/博通/台积电/美光/Arm…）的 8-K 1.01/2.01 条目。
    只抓带正文的条目，flows.py 可直接从正文抽边。
    """
    end = datetime.now()
    begin = end - timedelta(days=max(1, days))
    startdt, enddt = begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    picked: dict[str, dict] = {}

    def add(s: dict, fid: str) -> None:
        adsh = s.get("adsh") or (fid.split(":")[0] if ":" in fid else "")
        filename = fid.split(":")[-1] if ":" in fid else ""
        if not adsh or not filename:
            return
        ft = s.get("file_type") or ""
        if _sec_score(ft) <= 0:
            return
        cur = picked.get(adsh)
        if cur is None or _sec_score(ft) > _sec_score(cur["_ft"]):
            picked[adsh] = {"_s": s, "_ft": ft, "_file": filename, "_adsh": adsh}

    for q, form in SEC_DEAL_QUERIES:
        url = ("https://efts.sec.gov/LATEST/search-index?"
               + urllib.parse.urlencode({"q": q, "forms": form,
                                         "startdt": startdt, "enddt": enddt}))
        try:
            j = json.loads(_sec_get(url, timeout=25).decode("utf-8", "ignore"))
        except Exception:
            continue
        n, qseen = 0, set()
        for h in (j.get("hits", {}).get("hits") or []):
            s = h.get("_source") or {}
            fid = h.get("_id") or ""
            adsh = s.get("adsh") or (fid.split(":")[0] if ":" in fid else "")
            if not s.get("file_date") or not adsh or adsh in qseen:
                continue
            qseen.add(adsh)
            add(s, fid)
            n += 1
            if n >= per_query:
                break
        time.sleep(0.2)

    # 关注清单：8-K/6-K 的 1.01（重大协议）/ 2.01（完成收购）
    deal_items = {"1.01", "1.02", "2.01", "2.03"}
    for cik, label in SEC_WATCH.items():
        try:
            j = json.loads(_sec_get(
                f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=25
            ).decode("utf-8", "ignore"))
            r = (j.get("filings") or {}).get("recent") or {}
            forms = r.get("form") or []
            dates = r.get("filingDate") or []
            accs = r.get("accessionNumber") or []
            docs = r.get("primaryDocument") or []
            items = r.get("items") or []
        except Exception:
            continue
        n = 0
        for i, form in enumerate(forms):
            day = dates[i] if i < len(dates) else ""
            if not day or day < startdt:
                break
            if form not in ("8-K", "8-K/A", "6-K", "SC 13D", "SC 13D/A"):
                continue
            its = set((items[i] if i < len(items) else "").split(","))
            if form.startswith("8-K") and not (its & deal_items):
                continue
            doc = docs[i] if i < len(docs) else ""
            if not doc or doc.endswith(".xml"):
                continue
            acc = accs[i] if i < len(accs) else ""
            picked.setdefault(acc, {"_s": {"form": form, "file_date": day,
                                           "display_names": [label], "ciks": [cik],
                                           "_trusted": True, "items": sorted(its)},
                                    "_ft": doc, "_file": doc, "_adsh": acc})
            n += 1
            if n >= per_cik:
                break
        time.sleep(0.15)

    out = []
    for adsh, v in picked.items():
        s = v["_s"]
        cik = (s.get("ciks") or ["0"])[0]
        name = _sec_name(s)
        if not name or name == "SEC filing":
            name = (s.get("display_names") or ["SEC"])[0]
            name = re.sub(r"\s*\(CIK \d+\)", "", name).strip()
        form = s.get("form") or s.get("root_forms") or s.get("file_type") or ""
        if isinstance(form, list):
            form = form[0] if form else ""
        link = _sec_link(cik, adsh, v["_file"])
        body = _sec_body(link)
        if not body or not _deal_hit(body):
            continue
        # 未知公司必须真在讲 AI（风险因素里提一次 "AI" 不算），否则是能源/生物公司噪声
        ai_density = len(_AI_RE.findall(body))
        if not (s.get("_trusted") or _known_ai(name) or ai_density >= 3):
            continue
        bits = [f"{s.get('file_date','')}", str(form)]
        if s.get("items"):
            bits.append("items " + ",".join(s["items"]))
        out.append({
            "title": f"[SEC {form}] {name}",
            "link": link,
            "desc": " · ".join(bits),
            "guid": f"sec-deal-{adsh}",
            "t_valid": (s.get("file_date") or "") + "T00:00:00Z",
            "_body": body,
        })
        time.sleep(0.15)
    out.sort(key=lambda x: x["t_valid"], reverse=True)
    return out


# ── 智谱专题：媒体面按关键词检索（东财全网新闻） ──
# 实测（2026-11-23）：
#   keyword=智谱  hitsTotal 5329；keyword=唐杰  hitsTotal 149
#   返回字段：date / title / content / mediaName / url，JSONP 包裹
def _em_search_page(keyword: str, page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
    param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default",
            "pageIndex": page, "pageSize": page_size,
            "preTag": "", "postTag": "",
        }},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=x&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    raw = _get(url, timeout=20, accept="*/*").decode("utf-8", "ignore")
    body = raw[raw.find("(") + 1:raw.rfind(")")]
    data = json.loads(body)
    res = (data.get("result") or {}).get("cmsArticleWebOld") or []
    return res, int(data.get("hitsTotal") or 0)


def fetch_news_search(keyword: str, pages: int = 2, page_size: int = 50, days: int = 30,
                      must_any: tuple = (), must_all: tuple = (), exclude: tuple = (),
                      must_title_any: tuple = ()) -> list[dict]:
    """东财新闻搜索 → 标准 entry。按标题/摘要做相关性过滤，按日期截断。

    must_any: 文本里至少命中一个；must_all: 必须全部命中；exclude: 命中任一即丢。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out, seen = [], set()
    for page in range(1, pages + 1):
        items, _total = _em_search_page(keyword, page, page_size)
        if not items:
            break
        for it in items:
            link = (it.get("url") or "").strip()
            if not link or link in seen:
                continue
            title = _plain(it.get("title") or "", 240)
            desc = _plain(it.get("content") or "", 600)
            if not title:
                continue
            if must_title_any and not any(w in title for w in must_title_any):
                continue
            blob = f"{title} {desc}"
            if must_all and not all(w in blob for w in must_all):
                continue
            if must_any and not any(w in blob for w in must_any):
                continue
            if exclude and any(w in blob for w in exclude):
                continue
            t = _cn_time(it.get("date") or "")
            if t[:10] < cutoff:
                continue
            seen.add(link)
            out.append({
                "title": title,
                "link": link,
                "desc": desc,
                "guid": link,
                "t_valid": t,
                "_body": desc,          # 搜索摘要已够生成一句话摘要，省一次正文抓取
                "_media": it.get("mediaName") or "",
            })
    out.sort(key=lambda x: x["t_valid"], reverse=True)
    return out


# ── 智谱专题：官网新闻（一手） ──
# 列表页服务端渲染，文章页正文是图片/附件，因此只取标题+日期；正文仍可点原文看。
def _parse_zhipu_cards(html: str, cutoff: str, seen: set) -> list[dict]:
    hits = list(re.finditer(r'href="(/zh/news/(\d+))"', html))
    pos = [m.start() for m in hits]
    out = []
    for k, m in enumerate(hits):
        nid = m.group(2)
        if nid in seen:
            continue
        # 每个 card 从本链接到下一个链接；图片 alt 就是标题，日期有 / 与 - 两种写法
        end = pos[k + 1] if k + 1 < len(pos) else min(len(html), m.start() + 12000)
        seg = html[m.start(): end]
        tm = re.search(r'alt="([^"]{4,120})"', seg)
        if not tm:
            tm = re.search(r'<h[23][^>]*>([^<]{4,120})</h[23]>', seg)
        dm = re.search(r'(20\d{2})[/-](\d{1,2})[/-](\d{1,2})', seg)
        if not tm or not dm:
            continue
        title = _plain(tm.group(1), 200).strip()
        date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        if not title or date < cutoff:
            continue
        seen.add(nid)
        out.append({
            "title": title,
            "link": "https://www.zhipuai.cn" + m.group(1),
            "desc": "",
            "guid": f"zhipu-news-{nid}",
            "t_valid": _cn_time(date + " 10:00:00"),
            "_no_fetch": True,
        })
    return out


def fetch_zhipu_news(limit: int = 60, days: int = 1500, pages: int = 4) -> list[dict]:
    """官网新闻列表。站点目前不接受 ?page=N（返回同一页），但保留多页探测：
    一旦官网以后支持翻页，这里会自动吃到更多条目；不支持时只跑一次，成本不变。"""
    base = "https://www.zhipuai.cn/zh/news"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    seen, out, prev = set(), [], None
    for page in range(1, max(1, pages) + 1):
        url = base if page == 1 else f"{base}?page={page}"
        try:
            html = _get(url, timeout=25, accept="text/html")
        except Exception:
            break
        if prev is not None and html == prev:
            break                      # 站点忽略 page 参数：不重复抓
        prev = html
        try:
            text = html.decode("utf-8", "ignore")
        except Exception:
            break
        got = _parse_zhipu_cards(text, cutoff, seen)
        if page > 1 and not got:
            break
        out.extend(got)
    out.sort(key=lambda x: x["t_valid"], reverse=True)
    return out[:limit]
