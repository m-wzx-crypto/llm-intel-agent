"""Hawkes / 泊松基线冒烟测试。"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import hawkes

BASE = datetime(2026, 1, 1, 9, 0, 0)


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def main():
    events = [_fmt(BASE + timedelta(days=i * 0.7)) for i in range(90)]
    r = hawkes.fit(events, min_events=60)
    assert r["status"] == "ok", r
    assert r["n_events"] == 90
    for k in ("mu_per_day", "alpha", "beta_per_day", "branching_ratio",
              "aic_hawkes", "aic_poisson", "better"):
        assert k in r, k
    assert 0 <= r["branching_ratio"] < 1
    print("PASS hawkes", r["n_events"], "events, branch=", r["branching_ratio"],
          "better=", r["better"])

    short = hawkes.fit(events[:10], min_events=60)
    assert short["status"] == "insufficient", short
    print("PASS insufficient gate:", short["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
