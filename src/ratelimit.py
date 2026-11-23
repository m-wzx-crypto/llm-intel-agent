"""出站限流 + 每日 token 预算闸门。

两件事必须分开，混在一起会调不准：

**限流**：控制"同时几个请求在路上"和"两次请求至少隔多久"。
  目的不是省钱，是别把对方打挂——一秒十几个并发打到模型网关或新闻站，
  轻则 429，重则封 IP，而我们第二天还要靠这些源吃饭。

**预算**：按天累计 token，到顶就抛 BudgetExceeded，让调用方**降级**（走规则摘要），
  而不是继续烧。降级是设计的一部分：没有模型的系统也得能出简报。

预算口径（客户明确要求）：**不看缓存命中，按全额计**。
  DeepSeek 返回的 prompt_tokens 已经**包含**缓存命中那部分（另有
  prompt_cache_hit_tokens 单独标出命中量），所以 prompt+completion 就是全额。
  只按"未命中部分"算会让预算看起来花得很少，实际账单不是那么回事。

为什么不用 asyncio：整条链路是同步阻塞式（urllib + sqlite3），
换异步要重写采集层，收益为零。信号量 + 最小间隔足够达到"不并发打爆"的目标。
"""
import threading
import time

# ── 预算 ──
DEFAULT_DAILY_BUDGET = 50_000_000      # 5 千万 token/天
_CFG_CACHE = {"at": 0.0, "budget": DEFAULT_DAILY_BUDGET, "rate": None}


class BudgetExceeded(RuntimeError):
    """今日 token 已用完。调用方应降级到规则路径，不要重试。"""


def _settings() -> tuple[int, dict]:
    """从配置读预算与限流参数，5 秒缓存（采集期间会被调用几十次）。"""
    now = time.time()
    if now - _CFG_CACHE["at"] < 5.0 and _CFG_CACHE["rate"] is not None:
        return _CFG_CACHE["budget"], _CFG_CACHE["rate"]
    budget, rate = DEFAULT_DAILY_BUDGET, {"max_concurrent": 2, "min_interval_ms": 450}
    try:
        import pipeline
        cfg = pipeline.load_config()
        sch = cfg.get("schedule") or {}
        budget = int(sch.get("token_budget_daily") or DEFAULT_DAILY_BUDGET)
        r = cfg.get("llm_rate") or {}
        rate = {"max_concurrent": max(1, min(8, int(r.get("max_concurrent") or 2))),
                "min_interval_ms": max(0, min(5000, int(r.get("min_interval_ms") or 450)))}
    except Exception:
        pass
    _CFG_CACHE.update(at=now, budget=budget, rate=rate)
    return budget, rate


def invalidate() -> None:
    """配置改了要立刻生效，别等 5 秒缓存过期。"""
    _CFG_CACHE["at"] = 0.0
    _CFG_CACHE["rate"] = None
    for gate in (LLM_GATE, IO_GATE):
        gate.reconfigure()
        gate._applied = (-1, -1) if gate._cfg_source == "llm" else gate._applied


class Throttle:
    """并发上限 + 最小间隔。

    两个约束同时成立才算"不并发打爆"：最多 N 个在飞，且相邻两次发起至少隔 X 毫秒。
    只限并发不限间隔不够——线程抢到锁的顺序不受控，容易在极短时间内连续放行一批，
    对方的速率限制照样会响。

    实现用 Condition + 计数器而不是 Semaphore：并发上限要能**热更新**（用户改了
    系统页的设置立即生效），而 Semaphore 在运行中被替换会丢失已持有的许可。
    """

    def __init__(self, name: str, max_concurrent: int = 2, min_interval_ms: int = 450):
        self.name = name
        self.max_concurrent = max_concurrent
        self.min_interval_ms = min_interval_ms
        self._cv = threading.Condition()
        self._active = 0
        self._next_at = 0.0
        self._applied = (max_concurrent, min_interval_ms)
        self._cfg_source = None      # "llm" 表示跟着配置走

    def bind_config(self, which: str) -> None:
        self._cfg_source = which

    def _refresh(self) -> None:
        """惰性读配置：不在 import 期就碰 pipeline（会撞循环导入）。"""
        if self._cfg_source != "llm":
            return
        _, rate = _settings()
        want = (rate["max_concurrent"], rate["min_interval_ms"])
        if want != self._applied:
            with self._cv:
                self.max_concurrent, self.min_interval_ms = want
                self._applied = want
                self._cv.notify_all()

    def reconfigure(self) -> None:
        self._refresh()

    def __enter__(self):
        self._refresh()
        with self._cv:
            while True:
                now = time.time()
                if self._active < self.max_concurrent and now >= self._next_at:
                    self._active += 1
                    self._next_at = now + self.min_interval_ms / 1000.0
                    return self
                wait = self._next_at - now
                self._cv.wait(timeout=min(0.5, wait) if wait > 0 else 0.05)

    def __exit__(self, *exc):
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify()
        return False


# LLM 网关：并发 2、间隔 450ms → 上限约 2 次/秒（实际受响应时间约束更慢）
LLM_GATE = Throttle("llm", 2, 450)
LLM_GATE.bind_config("llm")   # 参数在首次使用时才从配置读，避免 import 期循环依赖

# 新闻站点：并发 4、间隔 120ms。对站点比对自己人宽松，但仍然不许并发轰炸
IO_GATE = Throttle("io", 4, 120)


# ── token 计量 ──
def billable(usage: dict) -> tuple[int, int]:
    """返回 (计入预算的 token, 其中缓存命中数)。

    全额口径：prompt(含缓存) + completion。缓存命中只用于展示"省了多少"。
    """
    u = usage or {}
    p = int(u.get("prompt_tokens") or 0)
    c = int(u.get("completion_tokens") or 0)
    total = p + c
    if total <= 0:
        total = int(u.get("total_tokens") or 0)
    hit = int(u.get("prompt_cache_hit_tokens")
              or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return total, hit


def check_budget(want: int = 0) -> None:
    """调用前过闸。已用 ≥ 预算就抛 BudgetExceeded。"""
    budget, _ = _settings()
    if budget <= 0:            # 0 = 不设上限
        return
    try:
        import db
        used = db.today_tokens()
    except Exception:
        return
    if used >= budget:
        raise BudgetExceeded(f"今日已用 {used:,} token，达到预算 {budget:,}，停止调用模型")


def status() -> dict:
    """给 /api/usage 与系统页用。"""
    budget, rate = _settings()
    try:
        import db
        used = db.today_tokens()
        detail = db.usage_detail()
    except Exception:
        used, detail = 0, {}
    return {"today_tokens": used, "budget": budget,
            "pct": round(used / budget * 100, 2) if budget > 0 else 0.0,
            "remaining": max(0, budget - used) if budget > 0 else None,
            "cached_tokens": detail.get("cached", 0),
            "reasoning_tokens": detail.get("reasoning", 0),
            "calls": detail.get("calls", 0),
            "rate": rate}
