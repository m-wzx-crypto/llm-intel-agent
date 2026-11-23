"""事件级去重合并：同一件事被多家报道 → 收敛成一条，保留全部来源。

为什么不能只比标题（真实踩过的坑，都是本库里真出现过的）：
  · 「深交所组合式推出新一批降费措施」vs「上交所组合式推出…」标题相似度 0.93，其实是两件事
  · 「涨停板的投资机会」vs「跌停板的投资机会」相似度 0.91，语义相反
  · 「开盘价 106 美元」vs「开盘价 110 美元」同名不同数，是两个数据点
  · 「XX公司回应」vs「YY公司回应」实体不同，不能并

所以是「一票通过 + 四道闸门否决」：

  相似度 = 0.65 × 标题bigram相似 + 0.35 × 正文bigram相似     （正文缺失时退化为纯标题）
  ① 对立词闸门：涨/跌、上交所/深交所、买/卖… 一侧有、另一侧反向 → 否决
  ② 数字闸门  ：两侧数字集合都非空且无交集 → 否决（不是同一个数据点）
  ③ 实体闸门  ：两侧公司集合都非空且无交集 → 否决
  ④ 同源闸门  ：同一家自己抄自己，需要更高相似度才算（防快讯流把不同时点并掉）

阈值分档：跨源 0.60，同源 0.85。
这是保守取向：宁可少并一条，不能把两件事并成一件——并错了会让「几家报道」这个
证据强度指标失真，而它正是本系统打分的基础。
"""
import json
import re
import unicodedata as ud
from datetime import datetime, timedelta

import entities as ENT

# ── 阈值 ──
TH_CROSS = 0.60        # 跨源合并阈值
TH_SAME = 0.85         # 同源合并阈值（同一家重复发）
MIN_TITLE_SIM = 0.45   # 标题相似度下限，正文再像也不能救
WINDOW_DAYS = 3        # 同一事件最多看前后 3 天

# 来源标签 / 尾部括注 / 栏目后缀：不剥掉会凭空造出差异
_TAG   = re.compile(r"^\s*[\[【][^\]】]{1,20}[\]】]\s*")          # [同花顺7x24] / [公告]
_WRAP  = re.compile(r"^\s*【([^】]{6,})】\s*(.*)$", re.S)               # 【标题本体】正文
_TAIL  = re.compile(r"\s*(?:[（(][^）)]{0,24}[）)])\s*$")
_DASH  = re.compile(r"\s*[|｜丨]\s*[^|｜丨]{0,14}$")
_DATE  = re.compile(r"(?:(\d{4})\s*[年\-/])?(\d{1,2})\s*[月\-/]\s*(\d{1,2})")

# 对立词对：同一对里两边各持一端 → 不是同一件事
OPPOSITE = [
    ("涨", "跌"), ("增", "减"), ("买", "卖"), ("多", "空"), ("盈", "亏"),
    ("上", "下"), ("松", "紧"), ("加息", "降息"), ("上调", "下调"),
    ("流入", "流出"), ("净买", "净卖"), ("中标", "流标"), ("批准", "否决"),
    ("通过", "驳回"), ("裁员", "扩招"), ("停牌", "复牌"), ("复产", "停产"),
    ("超预期", "不及预期"), ("提价", "降价"), ("解禁", "增持"), ("利好", "利空"),
]
# 交易所/市场互斥（不构成「上/下」对立，单独列）
EXCHANGES = ["上交所", "深交所", "北交所", "港交所", "纽交所", "纳斯达克"]

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ALNUM = re.compile(r"[a-z0-9]+")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def strip_prefix(title: str) -> str:
    """只剥来源标签，保留日期等判别信息（闸门用这个）。"""
    return _TAG.sub("", title or "").strip()


def headline(title: str) -> str:
    """抽出「标题本体」，用于可比性（相似度用这个）。

    快讯源的标题常是「[快讯台] 【标题本体】正文开头」或「标题 ｜栏目」，
    直接比会把正文开头和栏目名算成差异，导致同一件事相似度被稀释。
    顺序：① 去 [来源] 前缀 ② 【X】Y 取 X ③ 去尾部括注与栏目后缀。
    """
    s = _TAG.sub("", (title or "").strip())
    m = _WRAP.match(s)
    if m and len(m.group(1).strip()) >= 6:
        s = m.group(1).strip()
    for _ in range(2):
        prev = s
        s = _DASH.sub("", s)
        s = _TAIL.sub("", s)
        s = s.strip()
        if s == prev:
            break
    return s


def tokens(text: str, limit: int = 0) -> frozenset:
    """中文 bigram + 拉丁词/数字，用于集合相似度。

    不用分词器：标准库没有，而 bigram 对中文新闻标题足够判别，
    且对「多/少一个字」天然鲁棒。
    """
    s = (text or "").lower()
    if limit:
        s = s[:limit]
    out = set()
    for run in _CJK.findall(s):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    for w in _ALNUM.findall(s):
        if len(w) > 1:
            out.add(w)
    return frozenset(out)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / (len(a) + len(b) - inter)


def _numbers(text: str) -> set:
    return {x.rstrip("0").rstrip(".") if "." in x else x for x in _NUM.findall(text or "")}


def _companies(text: str) -> set:
    res = ENT.resolve(strip_prefix(text))
    out = set()
    for e in res["all_entities"]:
        co = ENT.company_of(e)
        if co:
            out.add(co)
    return out


def conflicts(a_title: str, b_title: str, a_body: str = "", b_body: str = "") -> str | None:
    """返回否决理由；None 表示通过。这是「不能并」的硬约束。

    用 strip_prefix（保留日期）而不是 headline：剥掉日期会让
    「华尔街见闻早餐 | 11月18日」和「…11月20日」变得一模一样。
    """
    ta, tb = strip_prefix(a_title), strip_prefix(b_title)

    # ① 对立词：一侧有涨、另一侧有跌，且互不交叉 → 相反的两件事
    for x, y in OPPOSITE:
        if (x in ta and y in tb and y not in ta and x not in tb) or \
           (y in ta and x in tb and x not in ta and y not in tb):
            return f"opposite:{x}/{y}"
    # 交易所互斥
    ea = {e for e in EXCHANGES if e in ta}
    eb = {e for e in EXCHANGES if e in tb}
    if ea and eb and not (ea & eb):
        return "opposite:exchange"

    # ② 日期不同 → 最典型的是每日固定栏目（早报/收评/快照），绝不能并
    da, db = _DATE.search(ta), _DATE.search(tb)
    if da and db and (da.group(2), da.group(3)) != (db.group(2), db.group(3)):
        return "date"

    # ③ 数字：两侧都有数字却完全无交集 → 不同数据点
    na, nb = _numbers(ta), _numbers(tb)
    if na and nb and not (na & nb):
        return "numbers"

    # ④ 公司实体：两侧都识别出公司且完全不重叠 → 不同主体
    ca, cb = _companies(f"{ta}\n{a_body}"), _companies(f"{tb}\n{b_body}")
    if ca and cb and not (ca & cb):
        return "entities"
    return None


def score(a: dict, b: dict) -> tuple[float, str]:
    """返回 (相似度, 计算依据)。

    正文相似度只「加分」不「减分」：同一件事被写成两篇长文时正文用词差异很大，
    加权平均反而把标题的强证据稀释掉。正文真正的价值是在标题很短的快讯上补证据。
    """
    ta, tb = tokens(headline(a["title"])), tokens(headline(b["title"]))
    jt = jaccard(ta, tb)
    if jt < MIN_TITLE_SIM * 0.8:
        return round(jt, 4), "title_only"
    ba, bb = tokens(a.get("content") or "", 600), tokens(b.get("content") or "", 600)
    if len(ba) >= 20 and len(bb) >= 20:
        jb = jaccard(ba, bb)
        return round(max(jt, 0.60 * jt + 0.40 * jb), 4), f"title{jt:.2f}+body{jb:.2f}"
    return round(jt, 4), f"title{jt:.2f}"


def is_same(a: dict, b: dict) -> tuple[bool, float, str]:
    """判断两条是否为同一事件。a/b: {title, content, source_name, t_valid}"""
    s, how = score(a, b)
    same_src = a.get("source_name") == b.get("source_name")
    # 同源跨天不合并：那是每日固定栏目（早间要闻汇总/华尔街见闻早餐/日内请重点关注），
    # 标题一模一样但说的是不同天的事。同源同天才可能是自己重发。
    if same_src and (a.get("t_valid") or "")[:10] != (b.get("t_valid") or "")[:10]:
        return False, s, "same_source_cross_day"
    th = TH_SAME if same_src else TH_CROSS
    if s < th:
        return False, s, f"below:{s:.2f}<{th}"
    why = conflicts(a["title"], b["title"], a.get("content") or "", b.get("content") or "")
    if why:
        return False, s, why
    return True, s, how


# ── 簇内择优：谁当代表 ──
TIER = {"量子位": 1, "机器之心": 1, "智谱": 1}


def _rank(d: dict):
    """排序键（越小越优）：有正文 > 一手源 > 事发早 > 正文更长"""
    return (
        0 if d.get("fetched_ok") else 1,
        TIER.get(d.get("source_name"), 2),
        d.get("t_valid") or "9",
        len(d.get("content") or "") * -1,
        d.get("id") or 0,
    )


def _load(conn, days: int = WINDOW_DAYS) -> list[dict]:
    since = (datetime.now() - timedelta(days=days + 1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT d.id, d.title, d.content, d.source_name, d.source_id, d.t_valid, d.fetched_ok, "
        "       d.coverage_count, i.entities, i.score "
        "FROM doc d LEFT JOIN item i ON i.doc_id = d.id "
        "WHERE d.dupe_of IS NULL AND substr(d.t_valid,1,10) >= ? "
        "ORDER BY d.t_valid DESC", (since,)).fetchall()
    return [dict(r) for r in rows]


def _union_find(pairs, n_ids):
    parent = {i: i for i in n_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups = {}
    for i in n_ids:
        groups.setdefault(find(i), []).append(i)
    return groups


def find_groups(docs: list[dict]) -> tuple[list[dict], dict]:
    """输入窗口内候选文档，输出合并簇（只保留 >1 条成员的）。

    分块：按自然日 ±1 天比较，避免全量 O(n²)；1840 条全库回溯约 0.4 秒。
    """
    by_day = {}
    for d in docs:
        by_day.setdefault((d.get("t_valid") or "")[:10], []).append(d)

    days = sorted(by_day)
    pairs, stats = [], {"pairs": 0, "blocked": {}}
    done = set()
    for day in days:
        pool = list(by_day[day])
        if WINDOW_DAYS > 1:
            i = days.index(day)
            for nxt in days[i + 1:i + WINDOW_DAYS]:
                pool += by_day[nxt]
        pool.sort(key=lambda x: x.get("t_valid") or "")
        for i in range(len(pool)):
            for k in range(i + 1, len(pool)):
                a, b = pool[i], pool[k]
                key = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
                if key in done:
                    continue
                done.add(key)
                stats["pairs"] += 1
                ok, s, how = is_same(a, b)
                if ok:
                    pairs.append((a["id"], b["id"]))
                elif how and not how.startswith("below:"):
                    stats["blocked"][how.split(":")[0]] = \
                        stats["blocked"].get(how.split(":")[0], 0) + 1
    groups = _union_find(pairs, [d["id"] for d in docs])
    by_id = {d["id"]: d for d in docs}
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        docs_in = sorted((by_id[m] for m in members), key=_rank)
        out.append({"canonical": docs_in[0], "members": docs_in})
    out.sort(key=lambda g: -len(g["members"]))
    return out, stats


def apply_groups(conn, groups: list[dict]) -> dict:
    """落库：canonical 保留，其余记 dupe_of + merge_sim；coverage 记去重后的来源家数。"""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    merged_docs = 0
    ent_added = 0
    for g in groups:
        canon = g["canonical"]
        cid = canon["id"]
        srcs = {canon["source_name"]}
        union_ents = list(json.loads(canon.get("entities") or "[]"))
        best_sim = 0.0
        for d in g["members"][1:]:
            ok, s, _ = is_same(canon, d)
            conn.execute("UPDATE doc SET dupe_of=?, merge_sim=?, merged_at=? WHERE id=?",
                         (cid, round(s, 4), now, d["id"]))
            merged_docs += 1
            srcs.add(d["source_name"])
            best_sim = max(best_sim, s)
            for e in ENT.resolve(strip_prefix(d["title"]))["entities"]:
                if e not in union_ents:
                    union_ents.append(e)
                    ent_added += 1
        conn.execute("UPDATE doc SET coverage_count=?, merged_at=? WHERE id=?",
                     (len(srcs), now, cid))
        # 代表条的实体取簇内并集：否则第二家标题里才出现的公司会漏进「按公司归类」
        conn.execute("UPDATE item SET entities=? WHERE doc_id=?",
                     (json.dumps(union_ents, ensure_ascii=False), cid))
    conn.commit()
    return {"stories": len(groups), "merged_docs": merged_docs, "entities_added": ent_added}


def run(conn=None, days: int = WINDOW_DAYS, dry_run: bool = False) -> dict:
    """跑一次合并。返回统计，供任务结果与验收脚本使用。"""
    import db as _db
    own = conn is None
    conn = conn or _db.connect()
    try:
        docs = _load(conn, days)
        groups, stats = find_groups(docs)
        res = {"scanned": len(docs), "compared_pairs": stats["pairs"],
               "blocked": stats["blocked"], "stories": len(groups),
               "merged_docs": sum(len(g["members"]) - 1 for g in groups),
               "dry_run": dry_run}
        if not dry_run:
            res.update(apply_groups(conn, groups))
            res["merged_docs"] = sum(len(g["members"]) - 1 for g in groups)
        res["samples"] = [
            {"keep": g["canonical"]["title"][:60],
             "sources": sorted({m["source_name"] for m in g["members"]}),
             "n": len(g["members"])}
            for g in groups[:8]
        ]
        return res
    finally:
        if own:
            conn.close()


def sources_of(conn, doc_id: int) -> list[dict]:
    """一条事件的全部来源（代表条 + 所有转载），供 UI「后面附上全部来源」。"""
    row = conn.execute("SELECT dupe_of FROM doc WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return []
    cid = row["dupe_of"] or doc_id
    rows = conn.execute(
        "SELECT id, source_name, title, url, t_valid, dupe_of, merge_sim "
        "FROM doc WHERE id=? OR dupe_of=? ORDER BY (id=?) DESC, t_valid",
        (cid, cid, cid)).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "source": r["source_name"], "title": r["title"],
                    "url": r["url"], "t": r["t_valid"],
                    "is_primary": r["dupe_of"] is None,
                    "sim": r["merge_sim"]})
    return out
