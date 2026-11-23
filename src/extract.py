"""抽取层：分类 / 打分 / 摘要。

按技术方案 §1：
- 检测与统计不用 LLM（这里全部规则化，可复现）
- LLM 只做最后一步「摘要」，且必须有降级路径（无 key 时用规则摘要）
"""
import json
import os
import re
import urllib.error
import urllib.request

import ratelimit
from ratelimit import BudgetExceeded

from sources import CATEGORIES, CATEGORY_PRIORITY, ENTITIES, EVENT_WORDS

MAX_SUMMARY = 120  # digest 里不许有长文本（方案 §6.1：≤40字为理想，这里放宽到 120 但 UI 截断）


def classify(title: str, content: str) -> dict:
    """分类 + 打分。实体识别走 entities.resolve（最长匹配优先，避免「北美豆包」→ 豆包）"""
    import entities as ENT
    blob = f"{title}\n{content}"
    res = ENT.resolve(blob)
    cats = res["categories"]
    ents = res["entities"]

    forced = ENT.title_category(title)
    if forced:
        category = forced
    elif not cats:
        category = "行业其他"
    else:
        # 命中数最多的板块；并列按优先级（智谱在前）
        category = sorted(cats.keys(), key=lambda c: (-len(cats[c]), CATEGORY_PRIORITY.get(c, 9)))[0]

    # ①「上市/融资」这类词单独出现不足以判定为资本事件（例：小鹏 G9L 上市）
    if category == "资本事件" and not res["entities"]:
        category = "行业其他"
    # ② 但「完成 X 轮融资 / 天使轮」是明确的资本事件，应压过顺带提到的赛道词
    if category != "智谱·唐杰" and re.search(r"融资|天使轮|轮融资|估值达|获投|领投", title):
        if "资本事件" in cats:
            category = "资本事件"

    events = []
    for ev, words in EVENT_WORDS.items():
        if any(w in blob for w in words):
            events.append(ev)

    # 排序：标题里出现的优先，其次按出现次数（不能用字符序，中文会被挤掉）
    low_title = title.lower()
    counts = res.get("counts") or {}
    ents.sort(key=lambda e: (0 if e.lower() in low_title else 1, -counts.get(e, 0), e))
    title_hits = sum(1 for e in ents if e.lower() in low_title)
    score = min(1.0, 0.22 * len(ents) + 0.30 * title_hits + 0.10 * len(events))

    return {
        "category": category if category in CATEGORIES else "行业其他",
        "score": round(score, 3),
        "keywords": ents[:8],
        "entities": ents[:8],
        "events": events,
    }


def rule_summary(title: str, content: str) -> str:
    """无 LLM 时的降级摘要：取正文前若干句，去掉噪声"""
    src = re.sub(r"<[^>]{0,300}>", " ", (content or "")).strip()
    src = re.sub(r"\s+", " ", src)
    if not src or len(src) < 10:
        return ""
    # 摘要与标题高度重复 → 视为无信息
    if len(src) < len(title) * 1.2 and (title[:12] in src):
        return ""
    src = re.sub(r"\s+", " ", src)
    # 去掉常见导语噪声
    for _ in range(3):
        src = re.sub(r"^(?:头图来源|题图来源|本文来自|原标题|来源|作者|编辑|文|图)[\s｜|:：/]{1,3}[^\s。，]{0,20}[。，\s]*", "", src).strip()
    parts = re.split(r"(?<=[。！？!?])", src)
    out = ""
    for p in parts:
        p = p.strip()
        if len(p) < 8:
            continue
        if len(out) + len(p) > MAX_SUMMARY:
            break
        out += p
    return (out or src[:MAX_SUMMARY]).strip()


class LLM:
    """OpenAI 兼容接口。默认 DeepSeek，可换智谱/自建。"""

    def __init__(self, cfg: dict):
        self.base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or "deepseek-chat"
        self.enabled = bool(self.key)
        self.tokens = 0              # 全额口径：prompt(含缓存命中) + completion
        self.cached_tokens = 0       # 其中有多少 prompt 命中了缓存（只作展示）
        self.reasoning_tokens = 0    # 其中有多少花在"思考"上（推理模型才有）
        self.budget_hit = False      # 今日预算用完 → 后续调用直接降级，不再打网络

    def chat_web(self, system: str, user: str, max_tokens=700) -> dict:
        """带 DeepSeek 服务端联网工具的对话（2026-11-13「全家桶应用 API」）。

        官方契约（Chat Completions）：
          tools: [{"type":"deepseek_web_search"},{"type":"deepseek_read_url"}]
          模型自己判断要不要搜；搜索/抓取都在服务端完成，客户端不执行工具。
        返回 {ok, text, citations, sources, queries, steps, error}
        注意：tool_choice 不能指定这两个工具，否则 400。
        """
        if not self.enabled:
            return {"ok": False, "error": "未配置 API Key"}
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "tools": [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        try:
            ratelimit.check_budget()
            with ratelimit.LLM_GATE:
                with urllib.request.urlopen(req, timeout=90) as r:
                    j = json.loads(r.read().decode("utf-8", "ignore"))
        except BudgetExceeded as e:
            self.budget_hit = True
            return {"ok": False, "error": str(e), "budget": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        if j.get("error"):
            return {"ok": False, "error": str(j["error"])[:300]}
        msg = ((j.get("choices") or [{}])[0]).get("message") or {}
        text = msg.get("content") or ""
        cites, sources, queries, steps = [], [], [], []
        for a in (msg.get("annotations") or []):
            uc = a.get("url_citation") or {}
            if uc.get("url"):
                cites.append({"title": uc.get("title") or uc["url"], "url": uc["url"]})
        for st in (msg.get("server_tool_calls") or []):
            for c in (st.get("calls") or []):
                act = c.get("action") or {}
                if act.get("type") == "search":
                    queries.extend(act.get("queries") or [])
                for s in (c.get("sources") or []):
                    if s.get("url"):
                        sources.append({"title": s.get("title") or s["url"],
                                        "url": s["url"], "snippet": (s.get("snippet") or "")[:160],
                                        "site": s.get("site_name") or ""})
                if c.get("page"):
                    sources.append({"title": c["page"].get("title") or c["page"].get("url", ""),
                                    "url": c["page"].get("url", ""), "snippet": "", "site": ""})
                steps.append({"kind": c.get("kind"), "action": act.get("type"), "status": c.get("status")})
        u = j.get("usage") or {}
        total, hit = ratelimit.billable(u)
        self.tokens += total
        self.cached_tokens += hit
        return {"ok": bool(text), "text": text, "citations": cites[:12],
                "sources": sources[:12], "queries": queries[:6], "steps": steps,
                "usage": u, "error": None if text else "模型未返回文本"}

    def chat(self, system: str, user: str, max_tokens=400) -> str:
        """一次对话。注意推理模型的坑：

        deepseek-flash / glm 这类推理模型会先花 token 思考，思考也计入 max_tokens。
        预算给小了（实测 220）会出现 finish_reason=length 且 content 为空——
        调用方看到空字符串会以为"模型没话说"，实际上是被自己的预算掐死的。
        所以：返回空且用满了预算时，自动放大预算重试一次。
        """
        if not self.enabled:
            raise RuntimeError("未配置 API Key")
        if self.budget_hit:
            raise BudgetExceeded("今日 token 预算已用完")
        budget = max_tokens
        for attempt in range(3):
            try:
                text, used_all = self._chat_once(system, user, budget)
            except BudgetExceeded:
                self.budget_hit = True
                raise
            if text or not used_all:
                return text
            budget = 900 if attempt == 0 else 3000   # 逐次放大思考空间
        return ""

    def _chat_once(self, system: str, user: str, max_tokens: int) -> tuple[str, bool]:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
            method="POST")
        ratelimit.check_budget()          # 预算用尽直接抛，不浪费一次请求
        with ratelimit.LLM_GATE:          # 并发上限 + 最小间隔，防止把网关打爆
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode())
        usage = data.get("usage") or {}
        total, hit = ratelimit.billable(usage)     # 全额：不看缓存命中
        self.tokens += total
        self.cached_tokens += hit
        self.reasoning_tokens += int((usage.get("completion_tokens_details") or {})
                                     .get("reasoning_tokens") or 0)
        ch = (data.get("choices") or [{}])[0]
        text = ((ch.get("message") or {}).get("content") or "").strip()
        return text, (not text) and ch.get("finish_reason") == "length"

    def summarize(self, title: str, content: str) -> str:
        sys_p = ("你是产业链情报分析师。用中文写一句话摘要，要求："
                 "不超过60字，只讲事实与主体动作，不评价不预测，不加引号。")
        usr = f"标题：{title}\n正文：{content[:2500]}"
        if self.budget_hit:                    # 预算已用完，别再试
            return rule_summary(title, content)
        try:
            s = self.chat(sys_p, usr, max_tokens=400)
            s = re.sub(r"^[\"'「【]|[\"'」】]$", "", s).strip()
            return (s or rule_summary(title, content))[:MAX_SUMMARY]
        except Exception:
            return rule_summary(title, content)

    def test(self) -> tuple[bool, str]:
        try:
            s = self.chat("只回一个字", "ping", max_tokens=5)
            return True, f"连接正常（模型 {self.model}，返回 {s[:10]!r}）"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "API Key 无效（401）"
            return False, f"HTTP {e.code}: {e.read()[:120].decode(errors='ignore')}"
        except Exception as e:
            return False, f"连接失败：{str(e)[:120]}"
