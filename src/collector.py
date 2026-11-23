"""采集层：RSS 解析 + 正文抓取。全标准库，无第三方依赖。

按技术方案 §1 感知层：
- 0 命中也是数据点 → 记录 zero_hit
- 解析失败、网络失败分别记录，绝不混成"没有新闻"
"""
import gzip
import io
import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
TIMEOUT = 20


class _TextExtractor(HTMLParser):
    """从 HTML 里抠纯文本，跳过 script/style/nav"""

    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if t:
                self.buf.append(t)

    def text(self):
        return "\n".join(self.buf)


def html_to_text(html: str, limit: int = 60000) -> str:
    p = _TextExtractor()
    try:
        p.feed(html or "")
    except Exception:
        pass
    txt = re.sub(r"<[^>]{0,300}>", " ", p.text())   # 清掉畸形标签
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    t = re.sub(r"\n{2,}", "\n", txt)
    return t[:limit]


def http_get(url: str, timeout: int = TIMEOUT) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, identity",
    })
    import ratelimit
    with ratelimit.IO_GATE:      # 并发 4 / 间隔 120ms：对站点礼貌，也不让本机线程打满
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
            encoding = r.headers.get("Content-Encoding")
    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return status, raw


def _localname(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _child_text(node, names) -> str:
    for ch in node:
        if _localname(ch.tag) in names:
            return (ch.text or "").strip()
    return ""


def _child_texts(node, names) -> list:
    """同名字段的全部取值：RSS 常常 description 只有一句、content:encoded 才是全文"""
    out = []
    for ch in node:
        if _localname(ch.tag) in names:
            t = (ch.text or "").strip()
            if t:
                out.append(t)
    return out


def _decode_xml(xml_bytes: bytes) -> str:
    """编码判定：不少国内源声明 gbk 但内容其实是 utf-8（创业邦），先按 utf-8 试"""
    m = re.search(rb'encoding=["\']([^"\']+)', xml_bytes[:300])
    enc = m.group(1).decode("ascii", "ignore") if m else ""
    if enc and enc.lower().replace("-", "") not in ("utf8",):
        try:
            return xml_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return xml_bytes.decode(enc, "ignore")
    return xml_bytes.decode(enc or "utf-8", "ignore")


def _lenient_feed(text: str) -> list[dict]:
    """容忍标签不闭合的 RSS（创业邦等国内源常见）；入参是已解码文本"""
    import re as _re

    def pick(block, tag):
        mm = _re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, _re.S | _re.I)
        if not mm:
            return ""
        v = mm.group(1).strip()
        v = _re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", v, flags=_re.S).strip()
        return v

    out = []
    for block in _re.findall(r"<item[\s>].*?</item>", text, _re.S | _re.I):
        title = html_to_text(pick(block, "title"), 200)
        link = pick(block, "link")
        desc = pick(block, "description") or pick(block, "content:encoded")
        guid = pick(block, "guid") or link
        date = pick(block, "pubDate") or pick(block, "dc:date")
        if title and link:
            out.append({"title": title, "link": link, "desc": html_to_text(desc, 800),
                        "guid": guid, "t_valid": _parse_date(date)})
    return out


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """兼容 RSS 2.0 / Atom；先做编码判定再解析，标签不闭合时走宽容模式"""
    text = _decode_xml(xml_bytes)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError) as e:
        lenient = _lenient_feed(text)
        if lenient:
            return lenient
        raise ValueError(f"XML 解析失败: {e}") from e

    entries = [n for n in root.iter() if _localname(n.tag) in ("item", "entry")]
    out = []
    for n in entries:
        title = _child_text(n, {"title"})
        link = _child_text(n, {"link"})
        if not link:
            for ch in n:
                if _localname(ch.tag) == "link":
                    link = ch.attrib.get("href", "")
                    break
        # 同一篇里 description 可能只有一句导语，content:encoded 才是全文：取最长的那份
        texts = _child_texts(n, ("description", "summary", "content", "encoded"))
        raw = max(texts, key=len) if texts else ""
        full = html_to_text(raw, 20000)
        if len(full) > 1200:                 # 全文才清洗；短摘要保持原样
            full = clean_body(full, title)
        desc = full[:800]
        guid = _child_text(n, {"guid", "id"}) or link
        date = _child_text(n, {"pubdate", "published", "updated", "date"})
        t_valid = _parse_date(date)
        if title and link:
            item = {
                "title": title,
                "link": link,
                "desc": desc,
                "guid": guid,
                "t_valid": t_valid,
            }
            # 全文足够长才当作正文，省一次文章抓取；短文摘要仍交给 fetch_article
            if len(full) >= 3000:
                item["_body"] = full
            out.append(item)
    return out


def _parse_date(s: str) -> str:
    if not s:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = s.strip()
    # RFC822 (RSS)
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    # ISO8601 (Atom)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def feed_title(xml_bytes: bytes) -> str:
    """RSS/Atom 的频道名：自定义 feed 用它当源名，比「公众号3」有用"""
    text = _decode_xml(xml_bytes)
    m = (re.search(r"<channel[^>]*>.*?<title[^>]*>(.*?)</title>", text, re.S | re.I)
         or re.search(r"<feed[^>]*>.*?<title[^>]*>(.*?)</title>", text, re.S | re.I))
    if not m:
        return ""
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
    t = html_to_text(t, 60).strip()
    return re.sub(r"^公众号\s*[·•]\s*", "", t)


def fetch_source(src: dict) -> dict:
    """抓一个源，返回 {status, entries, http_status, latency_ms, error}

    status 四态：
      ok           有内容
      zero_hit     通道通但 0 条（本身是数据点）
      unavailable  网络/HTTP 失败
      parse_broken 拿到了但解析不出（站点改版）
    """
    t0 = time.time()
    try:
        code, raw = http_get(src["url"])
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {"status": "unavailable", "entries": [], "http_status": None,
                "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}
    ms = int((time.time() - t0) * 1000)
    if code >= 400:
        return {"status": "unavailable", "entries": [], "http_status": code,
                "latency_ms": ms, "error": f"HTTP {code}"}
    try:
        entries = parse_feed(raw)
    except ValueError as e:
        return {"status": "parse_broken", "entries": [], "http_status": code,
                "latency_ms": ms, "error": str(e)[:200]}
    return {"status": "ok" if entries else "zero_hit", "entries": entries,
            "http_status": code, "latency_ms": ms, "error": None,
            "feed_title": feed_title(raw)}


def fetch_article(url: str, limit: int = 60000) -> str:
    """抓正文：多候选容器各抽一遍，取最长的那份。

    旧实现用 re.search 取"第一个 <article> 或 content 容器"，非贪婪匹配经常只拿到片段，
    再加上 6000 字上限，导致库里的正文远短于原文（实测 10356 字只存了 6000）。
    """
    try:
        code, raw = http_get(url, timeout=20)
        if code >= 400:
            return ""
        html = raw.decode("utf-8", errors="ignore")

        candidates = []
        # ① 公众号正文容器
        m = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*(?:<script|</div>)', html, re.S | re.I)
        if m:
            candidates.append(m.group(1))
        # ② <article>（贪婪，覆盖多个 article）
        for m in re.finditer(r'<article[^>]*>(.*?)</article>', html, re.S | re.I):
            candidates.append(m.group(1))
        # ③ 常见内容容器 class/id
        for m in re.finditer(
                r'<(?:div|section)[^>]+(?:id|class)="[^"]*'
                r'(?:rich_media_content|article-content|article_content|post-content|'
                r'content-body|entry-content|main-content|article|content)[^"]*"[^>]*>(.*?)</(?:div|section)>',
                html, re.S | re.I):
            candidates.append(m.group(1))
        # ④ 兜底：整页
        candidates.append(html)

        best = ""
        for cand in candidates:
            txt = html_to_text(cand, limit)
            if len(txt) > len(best):
                best = txt
        return best
    except Exception:
        return ""

# ── 正文清洗：去掉网页模板/导航/免责声明 ──
BOILER = [re.compile(x) for x in [
    r"您正在使用.*?浏览器", r"为了您的.*?账号安全", r"强烈建议使用.*?浏览器",
    r"更快更安全的浏览器", r"此为临时链接", r"仅用于文章预览", r"文章预览，将在",
    r"^\s*正文\s*$", r"发私信给", r"^\s*发送\s*$", r"^\s*0\s*$",
    r"^\s*本文作者[:：]?\s*$", r"^\s*导语[:：]", r"^\s*要闻提示\s*$",
    r"责任编辑[:：]", r"免责声明", r"扫码关注", r"长按识别", r"点击.{0,8}(查看|阅读)原文",
    r"未经授权.{0,10}转载", r"版权.{0,8}所有", r"热门推荐", r"相关阅读", r"相关文章",
    r"往期回顾", r"更多精彩", r"关注我们", r"订阅我们", r"APP下载", r"下载APP",
    r"^\s*原标题[:：]", r"^\s*来源[:：]", r"^\s*编辑[:：]", r"^\s*校对[:：]",
    r"^\s*上一篇", r"^\s*下一篇", r"^\s*相关推荐", r"^\s*热门文章",
    r"^\s*题图来源", r"^\s*(分享|点赞|在看|收藏|评论|举报)\s*$",
    r"^\s*[\d,]+\s*(阅读|浏览|次阅读)\s*$", r"^\s*扫描二维码.*$",
    r"^\s*本文来自.*$", r"^\s*戳.*链接.*$", r"^\s*END\s*$",
    r"^(Paragraph|Image|Table|Chart|Figure)[-_\d]", r"Static Table",
    r"^To view this video", r"^web browser that supports HTML5",
]]


def _norm(s: str) -> str:
    import unicodedata as ud
    return "".join(ch for ch in (s or "").lower()
                   if ud.category(ch)[0] not in ("P", "Z", "S", "C"))


def _title_like(line: str, title: str) -> bool:
    """这一行是不是标题/标题+站点名的重复"""
    if not title:
        return False
    a, b = _norm(line), _norm(title)
    if not a:
        return False
    return a == b or b.startswith(a) or a.startswith(b) or (len(a) > 8 and a[:12] == b[:12])


def clean_body(text: str, title: str = "") -> str:
    """去掉网页模板与导航碎片，保留正文。

    实测雷峰网/36氪 的正文前面会混进：
      「您正在使用IE低版浏览器…」「此为临时链接，仅用于文章预览…」「发私信给」「正文」
    这些都必须在入库前清掉，否则抽屉里展示的「原文」是脏的。
    """
    if not text:
        return ""
    lines = []
    for raw in text.split("\n"):
        ln = raw.strip()
        if not ln:
            continue
        if any(p.search(ln) for p in BOILER):
            continue
        if re.fullmatch(r"[\d\s:：\-—/年月日.]{4,20}", ln):   # 纯时间戳
            continue
        ln = re.sub(r"\s*[|｜]\s*[^|｜]{1,12}$", "", ln)        # 去掉 "标题 | 站点名" 的尾巴
        if len(ln) <= 4:                                       # 短碎片
            continue
        lines.append(ln)

    # 砍掉开头：短行、标题重复行、作者行（最多砍 15 行）
    for _ in range(15):
        if not lines:
            break
        head = lines[0]
        if len(head) < 20 or _title_like(head, title):
            lines.pop(0)
        else:
            break

    # 砍掉尾部（免责/推荐区）
    for j in range(len(lines) - 1, max(len(lines) - 12, 0), -1):
        if re.search(r"免责声明|热门推荐|相关阅读|往期回顾|更多精彩|版权", lines[j]):
            lines = lines[:j]
            break

    # 去相邻重复行
    out = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return "\n".join(out)
