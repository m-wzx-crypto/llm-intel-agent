"""预测台账：生成可结算命题 + 到期结算。

**这一版只用统计基线，不做产业判断**：
  命题形式：「未来 7 天，实体 X 的相关报道 ≥ N 条」
  N 由过去窗口的滚动分布定（保守：取过去 7 天均值 × 0.8）
  p 由历史频率估：过去 60 天里，滚动 7 天窗口达到 N 的比例
  到期用真实入库数据核对，写 resolved_hit / resolved_miss / void

为什么不用 LLM 生成：LLM 会写"某公司将于 12 月发布 X"这种无法自动核对的命题，
无法结算的预测等于没有预测。统计命题**能被数据打脸**，这才是台账的意义。
LLM 之后可以接，但必须走同一套结算器。
"""
import json
from datetime import datetime, timedelta, timezone

import db

WINDOW_DAYS = 7          # 预测窗口
LOOKBACK = 60            # 估 p 用的历史长度
CONSERVATIVE = 0.8       # 阈值打折系数


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_entity(conn, entity: str, start: datetime, end: datetime) -> int:
    n = conn.execute(
        "SELECT count(*) FROM item i JOIN doc d ON d.id=i.doc_id "
        "WHERE d.dupe_of IS NULL AND i.entities LIKE ? AND d.t_valid >= ? AND d.t_valid < ?",
        (f'%"{entity}"%', _iso(start), _iso(end))).fetchone()[0]
    return int(n)


def _hist_p(conn, name: str, threshold: int) -> tuple[float, int, int]:
    hits = tot = 0
    now = _utc_now()
    for k in range(1, LOOKBACK // WINDOW_DAYS + 1):
        end = now - timedelta(days=7 * (k - 1))
        start = end - timedelta(days=WINDOW_DAYS)
        c = _count_entity(conn, name, start, end)
        tot += 1
        if c >= threshold:
            hits += 1
    return round(min(0.9, max(0.1, (hits + 1) / (tot + 2))), 2), hits, tot


def _rule_challenge(name: str, threshold: int, last7: int, p: float) -> str:
    return (f"风险：过去 7 天 {last7} 条里可能含一次性发布会/融资脉冲；"
            f"若未来一周没有新节点，{threshold} 条可能达不到（历史命中率 {p:.0%}）。")


def _llm_challenge(name: str, threshold: int, last7: int, p: float) -> tuple[str, str]:
    """让模型唱反调：指出这条统计命题最可能失败的原因。失败则回退规则。"""
    rule = _rule_challenge(name, threshold, last7, p)
    try:
        import pipeline
        from extract import LLM
        cfg = pipeline.apply_active_profile(pipeline.load_config())
        llm = LLM(cfg.get("llm") or {})
        if not llm.enabled or llm.budget_hit:
            return rule, "rule"
        text = llm.chat(
            "你是唱反调的审稿人。只指出可证伪的失败原因，不给建议，60 字以内。",
            f"命题：未来 7 天「{name}」相关报道 ≥ {threshold} 条。"
            f"依据：过去 7 天 {last7} 条，历史窗口命中率 {p:.0%}。"
            "这条命题最可能因为什么而不成立？", 180)
        try:
            db.add_tokens(getattr(llm, "tokens", 0), "predict",
                          getattr(llm, "cached_tokens", 0),
                          getattr(llm, "reasoning_tokens", 0))
        except Exception:
            pass
        text = (text or "").strip()
        return (text[:200] or rule), "llm" if text else "rule"
    except Exception:
        return rule, "rule"


def propose(entity: str | None = None, limit: int = 3, use_llm: bool = False) -> list[dict]:
    """生成候选命题（不落库，返回给用户确认 —— B 级动作）"""
    conn = db.connect()
    try:
        now = _utc_now()
        # 选实体：默认取窗口内提及最多的
        if not entity:
            rows = conn.execute(
                "SELECT i.entities FROM item i JOIN doc d ON d.id=i.doc_id "
                "WHERE d.dupe_of IS NULL AND d.t_valid >= ?",
                (_iso(now - timedelta(days=14)),)).fetchall()
            cnt = {}
            for r in rows:
                for e in json.loads(r["entities"] or "[]"):
                    cnt[e] = cnt.get(e, 0) + 1
            top = sorted(cnt.items(), key=lambda kv: -kv[1])[:limit]
        else:
            top = [(entity, 0)]

        out = []
        for name, _ in top[:limit]:
            # 过去 7 天实际条数 → 阈值打八折
            last7 = _count_entity(conn, name, now - timedelta(days=7), now)
            if last7 == 0:
                continue
            threshold = max(1, round(last7 * CONSERVATIVE))

            p, hits, tot = _hist_p(conn, name, threshold)
            challenge, reviewed_by = (_llm_challenge(name, threshold, last7, p)
                                      if use_llm else
                                      (_rule_challenge(name, threshold, last7, p), "rule"))
            out.append({
                "claim_text": f"未来 {WINDOW_DAYS} 天，「{name}」相关报道 ≥ {threshold} 条",
                "entity": name,
                "deadline": (now + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d"),
                "p": p,
                "level": "B",          # 必须人工确认才落库
                "challenge": challenge,
                "reviewed_by": reviewed_by,
                "basis": {"entity": name, "threshold": threshold, "last7": last7,
                          "hist_windows": tot, "hist_hits": hits, "kind": "stat_baseline"},
            })
        return out
    finally:
        conn.close()


def propose_llm(entity: str | None = None, limit: int = 3) -> list[dict]:
    """让模型在候选实体里挑命题参数（阈值系数），仍走同一套结算器。"""
    import pipeline
    from extract import LLM
    base = propose(entity=entity, limit=limit, use_llm=True)
    if not base:
        return base
    try:
        cfg = pipeline.apply_active_profile(pipeline.load_config())
        llm = LLM(cfg.get("llm") or {})
        if not llm.enabled or llm.budget_hit:
            return base
        cand = [{"entity": x["entity"], "last7": x["basis"]["last7"],
                 "rule_threshold": x["basis"]["threshold"]} for x in base]
        text = llm.chat(
            "你是预测命题的参数审稿人。只能输出 JSON 数组，不要解释。",
            "候选实体与近 7 天条数：" + json.dumps(cand, ensure_ascii=False) +
            "。请为每条给一个 multiplier（0.5~1.2，相对近 7 天条数）和 why（20 字内）。"
            '输出：[{"entity":"...","multiplier":0.8,"why":"..."}]', 500)
        try:
            db.add_tokens(getattr(llm, "tokens", 0), "predict",
                          getattr(llm, "cached_tokens", 0),
                          getattr(llm, "reasoning_tokens", 0))
        except Exception:
            pass
        parsed = {}
        for x in _parse_json_list(text):
            if isinstance(x, dict) and x.get("entity") in {c["entity"] for c in cand}:
                try:
                    m = float(x.get("multiplier") or 0)
                except Exception:
                    continue
                if 0.5 <= m <= 1.2:
                    parsed[x["entity"]] = (m, str(x.get("why") or "")[:40])
        if not parsed:
            return base
        conn = db.connect()
        try:
            out = []
            for x in base:
                hit = parsed.get(x["entity"])
                if not hit:
                    out.append(x)
                    continue
                mult, why = hit
                last7 = x["basis"]["last7"]
                th = max(1, round(last7 * mult))
                p, hits, tot = _hist_p(conn, x["entity"], th)
                x = dict(x)
                x.update({
                    "claim_text": f"未来 {WINDOW_DAYS} 天，「{x['entity']}」相关报道 ≥ {th} 条",
                    "p": p,
                    "challenge": why or x.get("challenge"),
                    "reviewed_by": "llm",
                    "basis": {**x["basis"], "threshold": th, "hist_hits": hits,
                              "hist_windows": tot, "kind": "llm_assisted",
                              "multiplier": mult},
                })
                out.append(x)
            return out
        finally:
            conn.close()
    except Exception:
        return base


def _parse_json_list(text: str) -> list:
    text = (text or "").strip()
    for c in (text, text[text.find("["):text.rfind("]") + 1] if "[" in text else ""):
        try:
            v = json.loads(c)
            if isinstance(v, list):
                return v
        except Exception:
            continue
    return []


def commit(items: list[dict]) -> int:
    """把确认过的命题落进台账（B 级动作，由用户点确认后调用）"""
    conn = db.connect()
    try:
        sid = conn.execute("SELECT MAX(id) FROM snapshot").fetchone()[0]
        n = 0
        for it in items:
            conn.execute(
                "INSERT INTO prediction (claim_text, deadline, p, snapshot_id, basis, state) "
                "VALUES (?,?,?,?,?, 'open')",
                (it["claim_text"], it["deadline"], it["p"], sid,
                 json.dumps(it.get("basis") or {}, ensure_ascii=False)))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def settle_due() -> list[dict]:
    """到期结算：拿真实数据核对，写命中/未中。不可核对的记 void。"""
    conn = db.connect()
    try:
        today = _utc_now().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM prediction WHERE state='open' AND deadline <= ?", (today,)).fetchall()
        done = []
        for r in rows:
            basis = json.loads(r["basis"] or "{}")
            ent, th = basis.get("entity"), basis.get("threshold")
            if not ent or not th:
                conn.execute("UPDATE prediction SET state='unresolvable', resolved_at=? WHERE id=?",
                             (db.now_iso(), r["id"]))
                done.append({"id": r["id"], "state": "unresolvable"})
                continue
            end = datetime.strptime(r["deadline"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            start = end - timedelta(days=WINDOW_DAYS, hours=0)
            actual = _count_entity(conn, ent, start, end)
            state = "resolved_hit" if actual >= th else "resolved_miss"
            conn.execute("UPDATE prediction SET state=?, resolved_at=?, resolved_by=? WHERE id=?",
                         (state, db.now_iso(), json.dumps({"actual": actual, "threshold": th},
                                                          ensure_ascii=False), r["id"]))
            done.append({"id": r["id"], "state": state, "actual": actual, "threshold": th})
        conn.commit()
        return done
    finally:
        conn.close()


def calibration() -> dict:
    """校准：已结算命题的 Brier 分数 + 命中率"""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT p, state FROM prediction WHERE state IN ('resolved_hit','resolved_miss')").fetchall()
        if not rows:
            return {"n": 0, "brier": None, "hit_rate": None}
        bs = sum((r["p"] - (1.0 if r["state"] == "resolved_hit" else 0.0)) ** 2 for r in rows) / len(rows)
        hr = sum(1 for r in rows if r["state"] == "resolved_hit") / len(rows)
        return {"n": len(rows), "brier": round(bs, 3), "hit_rate": round(hr, 3),
                "baseline_brier": 0.25}
    finally:
        conn.close()
