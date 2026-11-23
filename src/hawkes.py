"""单变量 Hawkes 自激过程（指数核）+ 泊松基线。

用途：判断「最近这波讨论」是随机波动，还是被前序事件自我激发的。
  λ(t) = μ + Σ_{t_k<t} α·exp(-β(t-t_k))
  - μ：外生强度（背景发生率）
  - α/β：分支比，越接近 1 自激越强
  - 与同均值泊松模型比 AIC / 对数似然，避免把普通波动说成「爆发」

实现为纯 Python（不引入 numpy/scipy）：坐标上升 + 多起点。事件量 < 2000 时毫秒级。
数据只有日期精度时，同一天多条会加微小抖动，结果按「日级点过程」解读。
"""
import math
from datetime import datetime

MIN_EVENTS = 60


def _ll(params, times, T):
    mu, alpha, beta = params
    if not (mu > 0 and alpha >= 0 and beta > 0):
        return -math.inf
    if alpha / beta >= 0.999:
        return -math.inf
    ll = 0.0
    A = 0.0
    prev = None
    for t in times:
        if prev is not None:
            A = math.exp(-beta * (t - prev)) * (1.0 + A)
        lam = mu + alpha * A
        if lam <= 0:
            return -math.inf
        ll += math.log(lam)
        prev = t
    ll -= mu * T
    ll -= (alpha / beta) * sum(1.0 - math.exp(-beta * (T - t)) for t in times)
    return ll


def _fit(times, T):
    n = len(times)
    base = max(1e-9, n / max(T, 1e-9))
    best = (-math.inf, (base, base * 0.5, 1.0))
    starts = []
    for m_mu in (0.3, 1.0, 3.0):
        for m_a in (0.4, 1.2):
            for b0 in (0.5, 1.0, 2.0, 5.0):
                starts.append((base * m_mu, max(1e-9, base * m_mu * m_a), b0))
    steps = (0.5, 0.7, 0.85, 1.15, 1.4, 2.0)
    fine = (0.92, 0.96, 1.04, 1.08)
    for s in starts:
        p = [math.log(max(1e-12, x)) for x in s]
        ll = _ll([math.exp(x) for x in p], times, T)
        for _ in range(30):
            moved = False
            for i in range(3):
                cur = p[i]
                for mult in steps:
                    p[i] = cur + math.log(mult)
                    ll2 = _ll([math.exp(x) for x in p], times, T)
                    if ll2 > ll + 1e-9:
                        ll, cur, moved = ll2, p[i], True
                p[i] = cur
            if not moved:
                break
        for i in range(3):
            cur = p[i]
            for mult in fine:
                p[i] = cur + math.log(mult)
                ll2 = _ll([math.exp(x) for x in p], times, T)
                if ll2 > ll + 1e-9:
                    ll, cur = ll2, p[i]
            p[i] = cur
        if ll > best[0]:
            best = (ll, tuple(math.exp(x) for x in p))
    return best


def _parse(ts: str):
    s = (ts or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def fit(events: list[str], min_events: int = MIN_EVENTS) -> dict:
    """events 为时间字符串列表，返回拟合结果或 insufficient。"""
    pts = []
    for x in events:
        d = _parse(x)
        if d:
            pts.append(d.timestamp() / 86400.0)
    pts = sorted(pts)
    if not pts:
        return {"status": "insufficient", "n_events": 0, "min_events": min_events,
                "reason": "没有可解析的事件时间"}
    # 归一到「距首个事件的天数」，否则补偿项 T-t 会溢出
    t0 = pts[0]
    uniq = []
    for i, t in enumerate(pts):
        uniq.append((t - t0) + i * 1e-7)   # 同一天多条加微小抖动，保证严格递增
    n = len(uniq)
    if n < min_events:
        return {"status": "insufficient", "n_events": n, "min_events": min_events,
                "reason": f"事件数不足（{n} < {min_events}），先不做 Hawkes 拟合"}
    T = uniq[-1]
    if T < 7:
        return {"status": "insufficient", "n_events": n, "min_events": min_events,
                "reason": f"时间跨度太短（{T:.1f} 天），无法区分自激与单次脉冲"}
    ll, (mu, alpha, beta) = _fit(uniq, T)
    ll_p = n * math.log(n / T) - n
    k = 3
    aic_h = 2 * k - 2 * ll
    aic_p = 2 * 1 - 2 * ll_p
    branch = alpha / beta
    return {
        "status": "ok", "n_events": n, "window_days": round(T, 1),
        "mu_per_day": round(mu, 6), "alpha": round(alpha, 6),
        "beta_per_day": round(beta, 4), "branching_ratio": round(branch, 4),
        "ll_hawkes": round(ll, 2), "ll_poisson": round(ll_p, 2),
        "lr_stat": round(2 * (ll - ll_p), 2),
        "aic_hawkes": round(aic_h, 2), "aic_poisson": round(aic_p, 2),
        "better": "hawkes" if aic_h < aic_p else "poisson",
        "self_exciting": bool(branch >= 0.3 and aic_h < aic_p),
        "note": "分支比 α/β 越接近 1 自激越强；AIC 更小者优先。日级数据有精度上限。",
    }
