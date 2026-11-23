"""混合检索：BM25 + 字符 n-gram + RRF 融合。

之前对话/搜索只有 SQL LIKE + 板块过滤，换个说法就找不到。这里补一层纯本地的
轻量检索（不依赖向量模型/外部服务）：

  - BM25：中文按「单字 + 双字」切，英文按词；标题 3 倍、摘要 2 倍、正文 1 倍；
  - 字符 n-gram：标题+摘要的字符双字组，倒排求重叠，抗错别字/语序变化；
  - RRF：两路排名倒数融合，避免单路分数尺度不一致。

索引按 MAX(doc.id) 自动失效，5 分钟兜底；数据量在 5000 篇内延迟可控。
"""
import math
import re
import threading
import time
from collections import Counter

import db

_LATIN = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_CACHE = {"key": None, "at": 0.0, "index": None}
_LOCK = threading.Lock()
MAX_DOCS = 6000


def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    out = _LATIN.findall(text)
    for run in _CJK.findall(text):
        out.extend(run)
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _ngrams(text: str) -> set[str]:
    text = (text or "").lower()
    grams = set()
    for run in _CJK.findall(text):
        grams.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
        if len(run) == 1:
            grams.add(run)
    grams.update(_LATIN.findall(text))
    return grams


class _Index:
    def __init__(self, rows: list[dict]):
        self.docs = rows
        self.tf: list[Counter] = []
        self.dl: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.ngram_docs: list[set[str]] = []
        self.ngram_post: dict[str, set[int]] = {}
        self.avgdl = 1.0
        for i, r in enumerate(rows):
            title = r.get("title") or ""
            summary = r.get("summary") or ""
            content = (r.get("content") or "")[:1500]
            toks = (_tokens(title) * 3) + (_tokens(summary) * 2) + _tokens(content)
            tf = Counter(toks)
            self.tf.append(tf)
            self.dl.append(max(1, len(toks)))
            for t, n in tf.items():
                self.postings.setdefault(t, []).append((i, n))
            grams = _ngrams(f"{title} {summary}")
            self.ngram_docs.append(grams)
            for g in grams:
                self.ngram_post.setdefault(g, set()).add(i)
        if self.dl:
            self.avgdl = sum(self.dl) / len(self.dl)

    def bm25(self, query: str, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        q = _tokens(query)
        if not q:
            return []
        n = len(self.docs)
        scores: dict[int, float] = {}
        for term in set(q):
            plist = self.postings.get(term)
            if not plist:
                continue
            df = len(plist)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, tf in plist:
                denom = tf + k1 * (1 - b + b * self.dl[i] / self.avgdl)
                scores[i] = scores.get(i, 0.0) + idf * tf * (k1 + 1) / denom
        return sorted(scores.items(), key=lambda x: -x[1])[:200]

    def ngram(self, query: str) -> list[tuple[int, float]]:
        q = _ngrams(query)
        if not q:
            return []
        hits: Counter = Counter()
        for g in q:
            for i in self.ngram_post.get(g, ()):
                hits[i] += 1
        out = []
        for i, n in hits.items():
            denom = math.sqrt(max(1, len(q)) * max(1, len(self.ngram_docs[i])))
            out.append((i, n / denom))
        return sorted(out, key=lambda x: -x[1])[:200]


def _load_index() -> _Index:
    conn = db.connect()
    try:
        key = tuple(conn.execute("SELECT MAX(id), COUNT(*) FROM doc").fetchone())
    finally:
        conn.close()
    now = time.monotonic()
    if _CACHE["index"] and _CACHE["key"] == key and now - _CACHE["at"] < 300:
        return _CACHE["index"]
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT d.id,d.title,d.url,d.source_id,d.source_name,d.t_valid,d.t_txn,"
            "d.content,i.summary,i.category,i.entities "
            "FROM doc d LEFT JOIN item i ON i.doc_id=d.id "
            "WHERE d.dupe_of IS NULL ORDER BY d.id DESC LIMIT ?", (MAX_DOCS,))]
    finally:
        conn.close()
    idx = _Index(rows)
    _CACHE.update(key=key, at=now, index=idx)
    return idx


def retrieve(query: str, k: int = 10, days: int = 0) -> list[dict]:
    """返回融合后的 top-k 文档。days>0 时只保留该时间窗内的条目。"""
    q = (query or "").strip()
    if not q:
        return []
    with _LOCK:
        idx = _load_index()

    bm = idx.bm25(q)
    ng = idx.ngram(q)
    if days > 0:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        keep = {i for i, r in enumerate(idx.docs)
                if (r.get("t_valid") or "")[:10] >= cutoff}
        bm = [(i, s) for i, s in bm if i in keep]
        ng = [(i, s) for i, s in ng if i in keep]

    fused: dict[int, float] = {}
    meta: dict[int, dict] = {}
    for name, ranked in (("bm25", bm), ("ngram", ng)):
        for rank, (i, score) in enumerate(ranked, 1):
            fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
            meta.setdefault(i, {})[name] = {"rank": rank, "score": round(score, 4)}
    out = []
    for i, s in sorted(fused.items(), key=lambda x: -x[1])[:max(1, min(k, 50))]:
        r = idx.docs[i]
        out.append({
            "id": r["id"], "title": r.get("title") or "", "url": r.get("url") or "",
            "source_id": r.get("source_id") or "", "source_name": r.get("source_name") or "",
            "t_valid": r.get("t_valid") or "", "t_txn": r.get("t_txn") or "",
            "category": r.get("category") or "", "summary": r.get("summary") or "",
            "score": round(s, 6), "signals": meta.get(i, {}),
        })
    return out
