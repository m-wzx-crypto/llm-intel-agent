"""本地 HTTP 服务：给前端提供 API。只用标准库。

安全：只绑定 127.0.0.1，不监听外部接口。
"""
import io
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chat as chat_mod
import db
import digest as digest_mod
import pipeline
from extract import LLM

STATE = {"running": False, "stage": "", "pct": 0, "msg": "", "last_result": None}
TASKS = []          # 任务流水（内存态，重启清空）
KIND_LABEL = {"collect": "采集一次", "dig": "深挖实体", "predict": "生成预测",
              "export": "导出简报", "dedupe": "去重合并", "weekly": "生成周报",
              "daily_report": "生成早报", "xhs": "小红书评论采集"}
_LOCK = threading.Lock()

if getattr(sys, "frozen", False):
    WEB_DIR = os.path.join(sys._MEIPASS, "web")
else:
    WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _run_async():
    def progress(stage, pct, msg):
        STATE.update(stage=stage, pct=pct, msg=msg)

    try:
        res = pipeline.run_daily(progress=progress)
        STATE["last_result"] = res
        if res.get("skipped") == "locked":
            STATE.update(stage="跳过", msg=res.get("reason") or "另一个采集正在运行")
    except Exception as e:
        STATE["last_result"] = {"error": str(e)[:400]}
    finally:
        STATE.update(running=False, stage="完成", pct=100)
        res = STATE.get("last_result") or {}
        for tk in reversed(TASKS):
            if tk["status"] == "running":
                if res.get("skipped"):
                    tk["status"] = "skipped"
                else:
                    tk["status"] = "done" if not res.get("error") else "failed"
                tk["meta"] = {**(tk.get("meta") or {}),
                              "new_items": res.get("new_items"),
                              "tokens": res.get("tokens")}
                break


def maybe_catchup() -> dict:
    """GUI 启动时调用：距上次成功采集 ≥20h 就自动补采一次（早报/晚报缺失时兜底）。"""
    try:
        fr = db.freshness()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:120]}
    if not fr.get("stale_20h"):
        return {"ok": False, "reason": "数据新鲜", "freshness": fr}
    with _LOCK:
        if STATE["running"]:
            return {"ok": False, "reason": "已有采集在跑", "freshness": fr}
        STATE.update(running=True, stage="补采", pct=1,
                     msg="距上次成功采集超过 20 小时，自动补采", last_result=None)
    TASKS.append({"type": "collect", "label": "启动补采", "status": "running",
                  "at": db.now_iso(), "meta": {}})
    threading.Thread(target=_run_async, daemon=True).start()
    return {"ok": True, "reason": "started", "freshness": fr}


def _bg(kind: str, fn) -> dict:
    """跑一个后台任务并登记到任务流水（前端「任务」页看得到）。"""
    tk = {"type": kind, "label": KIND_LABEL.get(kind, kind), "status": "running",
          "at": db.now_iso(), "meta": {}}
    TASKS.append(tk)

    def worker():
        try:
            res = fn()
            tk["status"] = "done"
            tk["meta"] = res if isinstance(res, dict) else {"result": str(res)[:300]}
        except Exception as e:
            tk["status"] = "failed"
            tk["meta"] = {"error": str(e)[:300]}

    import threading as _th
    _th.Thread(target=worker, daemon=True).start()
    return tk


def _llm_for_cfg():
    """按当前激活档案构造一个 LLM 实例（周报写总结用）。"""
    cfg = pipeline.apply_active_profile(pipeline.load_config())
    return LLM(cfg.get("llm") or {})


def _valid_report_date(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date_cls.fromisoformat(value).isoformat() == value
    except Exception:
        return False


def _do_daily_report(report_date: str | None = None, force: bool = True) -> dict:
    import daily_report
    import locks
    handle = locks.acquire("data", blocking=False)
    if handle is None:
        raise RuntimeError("采集正在运行，请完成后再生成日报")
    try:
        llm = _llm_for_cfg()
        try:
            report = daily_report.ensure_report(report_date=report_date, llm=llm, force=force)
        finally:
            used = getattr(llm, "tokens", 0)
            try:
                db.add_tokens(used, "daily_report",
                              getattr(llm, "cached_tokens", 0),
                              getattr(llm, "reasoning_tokens", 0))
            except Exception:
                pass
        return {"generated": True, "report_date": report["report_date"],
                "mode": report["mode"], "tokens": used}
    finally:
        locks.release(handle)


def _do_dedupe() -> dict:
    import dedupe
    return dedupe.run(days=10)


def _do_weekly(week: str | None = None, force: bool = True) -> dict:
    import weekly
    llm = _llm_for_cfg()
    p = weekly.ensure_report(week=week, llm=llm, force=force) if week else weekly.build(llm=llm)
    try:
        db.add_tokens(getattr(llm, "tokens", 0), "weekly",
                      getattr(llm, "cached_tokens", 0), getattr(llm, "reasoning_tokens", 0))
    except Exception:
        pass
    return {"week": p["week"], "companies": len(p["groups"]),
            "stories": p["totals"]["stories"], "mode": p["mode"],
            "tokens": getattr(llm, "tokens", 0)}


def _xhs_brief() -> dict:
    import social
    st = social.collect_state()
    return {"mode": social.get_mode(), "cache_at": st.get("cache_at"),
            "comments": st.get("cache_comments"), "panels": st.get("cache_panels"),
            "keywords": st.get("keywords")}


def _ledger(limit: int = 10):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, claim_text, deadline, p, state, basis FROM prediction "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静音

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)

        if p in ("/", "/index.html"):
            fp = os.path.join(WEB_DIR, "index.html")
            if not os.path.exists(fp):
                self.send_error(404, "index.html missing")
                return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 绝不缓存：更新 app 后浏览器若沿用旧 HTML，用户会看到旧界面（踩过）
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
            return

        if p == "/api/status":
            # 采集期间数据库有写锁：这里任何一项失败都不许让接口 500，
            # 否则前端每 3 秒的轮询会连环报错（曾经就是这样刷屏的）。
            def _safe(fn, default=None):
                try:
                    return fn()
                except Exception as e:
                    print(f"[warn] /api/status 降级: {e}", file=sys.stderr)
                    return default
            try:
                cfg = pipeline.load_config()
            except Exception:
                cfg = {}
            _json(self, {
                "running": STATE["running"],
                "stage": STATE["stage"],
                "pct": STATE["pct"],
                "msg": STATE["msg"],
                "last_result": STATE["last_result"],
                "last_run": _safe(db.last_run),
                "freshness": _safe(db.freshness, {}),
                "llm_configured": bool((cfg.get("llm") or {}).get("api_key")),
                "llm_model": (cfg.get("llm") or {}).get("model"),
                "data_dir": _safe(db.app_dir, ""),
                "seed": _safe(db.seed_status, {"seeded": False}),
                "locked": _safe(lambda: __import__("locks").held(), False),
                "xhs": _safe(_xhs_brief, {}),
                "wechat": _safe(lambda: __import__("wechat_guard").cooldown_status(), {}),
            })
            return

        if p == "/api/qr":
            # 只为「扫码去哪申请/导出」生成二维码，不承担任何授权语义。
            text = (q.get("text", [""])[0] or "").strip()
            if not text or len(text) > 512 or not text.startswith(("http://", "https://")):
                self.send_error(400, "bad text")
                return
            try:
                import segno
                buf = io.BytesIO()
                segno.make(text, error="m").save(
                    buf, kind="svg", scale=5, border=2,
                    dark="#0f172a", light="#ffffff")
                body = buf.getvalue()
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if p == "/api/digest":
            cfg = pipeline.load_config()
            wd = int(q.get("days", [cfg["ui"]["window_days"]])[0])
            pp = int(q.get("per", [cfg["ui"]["per_panel"]])[0])
            try:
                _json(self, digest_mod.build_digest(wd, pp))
            except Exception as e:
                _json(self, {"error": str(e)[:300]}, 500)
            return

        if p == "/api/detail":
            try:
                _json(self, chat_mod.detail(int(q.get("id",["0"])[0])))
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            return

        if p == "/api/items":
            date = q.get("date", [""])[0]
            entity = q.get("entity", [""])[0]
            limit = int(q.get("limit", ["30"])[0])
            conn = db.connect()
            try:
                sql = ("SELECT i.id, i.category, i.score, i.summary, d.title, d.url, "
                       "d.source_name, d.t_valid, d.coverage_count FROM item i "
                       "JOIN doc d ON d.id=i.doc_id WHERE d.dupe_of IS NULL")
                args = []
                if date:
                    sql += " AND substr(d.t_valid,1,10)=?"
                    args.append(date)
                if entity:
                    sql += " AND (d.title LIKE ? OR d.content LIKE ?)"
                    args += [f"%{entity}%", f"%{entity}%"]
                sql += " ORDER BY i.score DESC, d.t_valid DESC LIMIT ?"
                args.append(limit)
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
                _json(self, {"items": rows, "date": date, "entity": entity})
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            finally:
                conn.close()
            return

        if p == "/api/weekly":
            import weekly
            try:
                week = (q.get("week", [""])[0] or "").strip()
                cur_s, cur_e, cur_lbl = weekly.week_bounds()
                rep = weekly.get(week) if week else (
                    weekly.latest_with_data() or weekly.get(cur_lbl))
                if rep is None:
                    # 还没生成过：先给一份「即时算好的」，不落库（避免空库首次访问也写盘）
                    rep = weekly.build(llm=None, save=False)
                _json(self, {"report": rep, "weeks": weekly.listing(),
                             "current_week": cur_lbl, "current_start": cur_s,
                             "current_end": cur_e})
            except Exception as e:
                _json(self, {"error": str(e)[:300]}, 500)
            return

        if p == "/api/predict":
            import predict
            use_llm = (q.get("llm", ["0"])[0] or "0") in ("1", "true", "yes")
            ent = (q.get("entity", [""])[0] or "").strip() or None
            _json(self, {"proposals": (predict.propose_llm(ent) if use_llm
                                       else predict.propose(ent)),
                         "calibration": predict.calibration(),
                         "ledger": _ledger()})
            return

        if p == "/api/usage":
            try:
                import ratelimit
                out = ratelimit.status()
            except Exception:
                out = {"today_tokens": db.today_tokens(), "budget": 0, "pct": 0,
                       "cached_tokens": 0, "reasoning_tokens": 0, "calls": 0,
                       "rate": {"max_concurrent": 2, "min_interval_ms": 450}}
            sch = (pipeline.load_config().get("schedule") or {})
            out.update({"guard": float(sch.get("evening_guard") or 0.8),
                        "morning": sch.get("morning") or "08:00",
                        "evening": sch.get("evening") or "20:00",
                        "slots": {}})
            try:
                conn = db.connect()
                try:
                    for r in conn.execute(
                            "SELECT id,started_at,status,new_items,note FROM run "
                            "WHERE status!='running' ORDER BY id DESC LIMIT 40"):
                        note = r["note"] or ""
                        slot = ""
                        for part in note.split():
                            if part.startswith("slot="):
                                slot = part[5:]
                        if slot and slot not in out["slots"]:
                            out["slots"][slot] = {"id": r["id"], "at": r["started_at"],
                                                  "status": r["status"], "new_items": r["new_items"]}
                        if len(out["slots"]) >= 2:
                            break
                finally:
                    conn.close()
            except Exception:
                pass
            _json(self, out)
            return

        if p == "/api/ablation":
            try:
                import ablation
                _json(self, ablation.run())
            except Exception as e:
                _json(self, {"error": str(e)[:300]}, 500)
            return

        if p == "/api/timeline":
            import metrics
            wd = int(q.get("days", ["14"])[0])
            kw = (q.get("q", [""])[0] or "").strip() or None
            try:
                _json(self, metrics.build_timeline(wd, kw))
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            return

        if p == "/api/competitors":
            import metrics
            wd = int(q.get("days", ["14"])[0])
            kw = (q.get("q", [""])[0] or "").strip() or None
            try:
                _json(self, metrics.build_competitors(wd, kw))
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            return

        if p == "/api/zhipu":
            import metrics
            wd = int(q.get("days", ["30"])[0])
            kw = (q.get("q", [""])[0] or "").strip() or None
            try:
                _json(self, metrics.build_zhipu(wd, kw))
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            return

        if p == "/api/social":
            import social
            wd = int(q.get("days", ["90"])[0])
            kw = (q.get("q", [""])[0] or "").strip() or None
            panel = (q.get("panel", ["zhipu"])[0] or "zhipu").strip().lower()
            try:
                _json(self, social.comment_report(wd, kw, panel))
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            return

        if p == "/api/xhs/status":
            import social
            import xhs_sidecar
            mode = social.get_mode()
            if mode == "local":
                # 内置 sidecar：是本机通道就先确保拉起（幂等，后台不阻塞）
                xhs_sidecar.ensure_async()
            engine = xhs_sidecar.state()
            _json(self, {"mode": mode,
                         "sidecar": engine.get("running", False),
                         "engine": engine,
                         "login": social.login_status(),
                         "collect": social.collect_state(),
                         "keywords": social.keywords(),
                         "panels": (pipeline.load_config().get("channels") or {}).get(
                             "xiaohongshu_panels") or social.DEFAULT_PANELS,
                         "base": social.XHS_BASE})
            return

        if p == "/api/xhs/qr":
            import social
            if not social.sidecar_alive():
                _json(self, {"ok": False, "reason": "本机通道未启动（xiaohongshu-mcp 未运行）"}, 200)
                return
            _json(self, social.login_qrcode())
            return

        if p == "/api/weibo/status":
            import weibo as weibo_mod
            _json(self, {"mode": weibo_mod.get_mode(),
                         "login": weibo_mod.login_status(),
                         "probe": weibo_mod.probe(),
                         "keywords": weibo_mod.keywords()})
            return

        if p == "/api/weibo/recent":
            import weibo as weibo_mod
            days = max(1, min(int(q.get("days", ["30"])[0]), 365))
            limit = max(1, min(int(q.get("limit", ["30"])[0]), 100))
            rows = []
            try:
                conn = db.connect()
                try:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT d.id, d.title, d.url, d.source_name, d.t_valid, d.content,"
                        " i.id AS item_id FROM doc d LEFT JOIN item i ON i.doc_id=d.id"
                        " WHERE d.source_id='weibo_local' AND d.dupe_of IS NULL"
                        " AND d.t_valid >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)"
                        " ORDER BY d.t_valid DESC LIMIT ?",
                        (f"-{days} days", limit)).fetchall()]
                finally:
                    conn.close()
            except Exception as e:
                _json(self, {"items": [], "error": str(e)[:160],
                             "mode": weibo_mod.get_mode(),
                             "login": weibo_mod.login_status()})
                return
            _json(self, {"items": rows, "mode": weibo_mod.get_mode(),
                         "login": weibo_mod.login_status()})
            return

        if p == "/api/weibo/qr":
            import weibo as weibo_mod
            try:
                _json(self, weibo_mod.qr_start())
            except Exception as e:
                _json(self, {"error": str(e)[:160]}, 200)
            return

        if p == "/api/entity":
            name = q.get("name", [""])[0]
            wd = int(q.get("days", ["30"])[0])
            try:
                import metrics as M
                data = M.build_competitors(wd)
                hit = next((c for c in data["competitors"] if c["name"] == name), None)
                _json(self, hit or {"error": "not found"})
            except Exception as e:
                _json(self, {"error": str(e)[:200]}, 500)
            return

        if p == "/api/channels":
            cfg = pipeline.load_config()
            import sources as SRC
            feeds = (cfg.get("channels") or {}).get("wechat_feeds") or []
            last = db.last_run()
            probes = {}
            if last:
                conn = db.connect()
                try:
                    for r in conn.execute(
                        "SELECT source_id,status,hit_count,latency_ms,error FROM probe_run WHERE run_id=?",
                        (last["id"],)):
                        probes[r["source_id"]] = dict(r)
                finally:
                    conn.close()
            srcs = []
            for s in SRC.SOURCES:
                pr = probes.get(s["id"], {})
                srcs.append({"id": s["id"], "name": s["name"], "group": s["group"],
                             "tier": s.get("tier", 3),
                             "status": pr.get("status", "not_run"),
                             "hits": pr.get("hit_count", 0),
                             "latency_ms": pr.get("latency_ms"),
                             "error": pr.get("error")})
            import wechat_presets
            arts = (cfg.get("channels") or {}).get("wechat_articles") or []
            if arts:
                pr = probes.get("wechat_direct", {})
                srcs.append({"id": "wechat_direct", "name": f"公众号直链（{len(arts)} 篇）",
                             "group": "wechat", "tier": 1,
                             "status": pr.get("status", "configured"),
                             "hits": pr.get("hit_count", 0)})
            for i, u in enumerate(feeds):
                pr = probes.get(f"wx{i}", {})
                srcs.append({"id": f"wx{i}",
                             "name": wechat_presets.name_of(u) or f"公众号订阅 {i+1}",
                             "group": "wechat", "tier": 1,
                             "status": pr.get("status", "configured"),
                             "hits": pr.get("hit_count", 0), "url": u})
            wm = (cfg.get("channels") or {}).get("weibo_mode", "off")
            wlogin = {}
            if wm == "local":
                try:
                    import weibo as weibo_mod
                    wlogin = weibo_mod.login_status()
                except Exception:
                    wlogin = {}
                pr = probes.get("weibo_local", {})
                srcs.append({"id": "weibo_local", "name": "微博", "group": "social", "tier": 2,
                             "status": pr.get("status") or (
                                 "ok" if wlogin.get("logged_in") else "need_login"),
                             "hits": pr.get("hit_count", 0)})
            _json(self, {
                "sources": srcs,
                "wechat_feeds": feeds,
                "wechat_presets": wechat_presets.catalog(feeds),
                "wechat_articles": (cfg.get("channels") or {}).get("wechat_articles") or [],
                "xiaohongshu_mode": (cfg.get("channels") or {}).get("xiaohongshu_mode", "off"),
                "weibo_mode": wm,
                "weibo_login": wlogin,
                "configurable": SRC.CONFIGURABLE,
            })
            return

        if p == "/api/tasks":
            hist = []
            try:
                conn = db.connect()
                try:
                    for r in conn.execute(
                        "SELECT id, started_at, finished_at, status, new_items, cost_tokens "
                        "FROM run ORDER BY id DESC LIMIT 20"):
                        hist.append({
                            "id": f"run-{r['id']}", "type": "每日采集",
                            "label": f"采集 #{r['id']}", "status": r["status"],
                            "started_at": r["started_at"], "finished_at": r["finished_at"],
                            "meta": {"new_items": r["new_items"], "tokens": r["cost_tokens"]},
                        })
                finally:
                    conn.close()
            except Exception as e:
                import sys as _s
                print(f"[warn] /api/tasks 读历史失败: {e}", file=_s.stderr)
            _json(self, {"tasks": TASKS[-30:][::-1] + hist,
                         "running": STATE["running"]})
            return

        if p == "/api/metrics":
            import metrics
            wd = int(q.get("days", [14])[0])
            kw = (q.get("q", [""])[0] or "").strip() or None
            try:
                _json(self, metrics.build_metrics(wd, kw))
            except Exception as e:
                _json(self, {"error": str(e)[:300]}, 500)
            return

        if p == "/api/search":
            import retrieval
            kw = (q.get("q", [""])[0] or "").strip()
            try:
                k = max(1, min(int(q.get("k", ["10"])[0]), 50))
                days = max(0, int(q.get("days", ["0"])[0]))
            except Exception:
                k, days = 10, 0
            _json(self, {"query": kw, "hits": retrieval.retrieve(kw, k, days)})
            return

        if p == "/api/hawkes":
            import hawkes
            days = max(7, min(int(q.get("days", ["90"])[0] or 90), 730))
            entity = (q.get("entity", [""])[0] or "").strip() or None
            try:
                min_ev = max(20, min(int(q.get("min_events", ["60"])[0]), 1000))
            except Exception:
                min_ev = hawkes.MIN_EVENTS
            cutoff = time.strftime("%Y-%m-%d",
                                   time.localtime(time.time() - days * 86400))
            try:
                conn = db.connect()
                try:
                    if entity:
                        rows = conn.execute(
                            "SELECT d.t_valid FROM doc d LEFT JOIN item i ON i.doc_id=d.id "
                            "WHERE d.dupe_of IS NULL AND substr(d.t_valid,1,10)>=? AND "
                            "(i.entities LIKE ? OR d.title LIKE ?) ORDER BY d.t_valid",
                            (cutoff, f'%"{entity}"%', f'%{entity}%')).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT t_valid FROM doc WHERE dupe_of IS NULL "
                            "AND substr(t_valid,1,10)>=? ORDER BY t_valid", (cutoff,)).fetchall()
                    events = [r[0] for r in rows]
                finally:
                    conn.close()
                res = hawkes.fit(events, min_ev)
                res.update({"entity": entity, "days": days})
                _json(self, res)
            except Exception as e:
                _json(self, {"status": "error", "entity": entity, "days": days,
                             "error": str(e)[:300]}, 500)
            return

        if p == "/api/reliability":
            import reliability
            days = max(1, min(int(q.get("days", ["30"])[0] or 30), 365))
            _json(self, reliability.build(days))
            return

        if p == "/api/sentiment":
            import sentiment
            days = max(1, min(int(q.get("days", ["30"])[0] or 30), 365))
            limit = max(1, min(int(q.get("limit", ["30"])[0] or 30), 100))
            _json(self, sentiment.review(days, limit))
            return

        if p == "/api/diagnostics":
            import diagnostics
            try:
                _json(self, diagnostics.build(STATE))
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)[:300]}, 500)
            return

        if p == "/api/config":
            cfg = pipeline.load_config()
            safe = json.loads(json.dumps(cfg))
            k = (safe.get("llm") or {}).get("api_key") or ""
            safe["llm"]["api_key"] = ("••••" + k[-4:]) if k else ""
            # 档案里的 Key 同样不能出本机
            for pr in safe.get("llm_profiles") or []:
                pk = pr.get("api_key") or ""
                pr["api_key"] = ("••••" + pk[-4:]) if pk else ""
            try:
                import secrets_store
                safe["keychain"] = secrets_store.status()
            except Exception:
                safe["keychain"] = {"available": False}
            _json(self, safe)
            return

        if p == "/api/daily-report":
            import daily_report
            requested = (q.get("date", [""])[0] or "").strip()
            if requested and not _valid_report_date(requested):
                _json(self, {"error": "日期格式应为 YYYY-MM-DD"}, 400)
                return
            try:
                report = daily_report.get(requested) if requested else daily_report.latest()
                if requested and report is None:
                    _json(self, {"error": "指定日期还没有日报"}, 404)
                    return
                _json(self, {"report": report, "reports": daily_report.listing(),
                             "date": report["report_date"] if report else requested})
            except Exception as e:
                _json(self, {"error": str(e)[:300]}, 500)
            return

        if p == "/api/daily-report/export":
            import daily_report
            requested = (q.get("date", [""])[0] or "").strip()
            if requested and not _valid_report_date(requested):
                _json(self, {"error": "日期格式应为 YYYY-MM-DD"}, 400)
                return
            try:
                report = daily_report.get(requested) if requested else daily_report.latest()
                if report is None:
                    _json(self, {"error": "还没有可导出的日报"}, 404)
                    return
                body = daily_report.to_markdown(report).encode("utf-8")
                filename = f"daily-report-{report['report_date']}.md"
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                _json(self, {"error": str(e)[:300]}, 500)
            return

        if p == "/api/export":
            cfg = pipeline.load_config()
            out = os.path.join(db.app_dir(), "digest.json")
            d = digest_mod.write_digest(out, cfg["ui"]["window_days"], cfg["ui"]["per_panel"])
            _json(self, {"ok": True, "path": out, "items": d["meta"]["items_in_window"]})
            return

        # ---- 远程推送更新（部署方案 §8.6）----
        if p == "/api/update/check":
            import updater
            force = (q.get("force", ["0"])[0] or "").lower() in ("1", "true", "yes")
            _json(self, updater.check(force=force))
            return

        if p == "/api/update/progress":
            import updater
            _json(self, {"ok": True, **updater.progress()})
            return

        if p == "/api/update/version":
            import updater
            _json(self, {"ok": True, **updater.info()})
            return

        self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}

        # ---- 远程推送更新（部署方案 §8.6）----
        if u.path == "/api/update/skip":
            import updater
            res = updater.skip((payload or {}).get("version"))
            _json(self, res, 200 if res.get("ok") else 400)
            return

        if u.path == "/api/update/download":
            import updater
            threading.Thread(target=updater.download, daemon=True).start()
            _json(self, {"ok": True, "msg": "开始下载"})
            return

        if u.path == "/api/update/install":
            import updater
            res = updater.install()
            _json(self, res, 200 if res.get("ok") else 400)
            if res.get("ok"):
                # 安装脚本在等当前进程退出：给前端留出读响应的窗口，然后硬退出
                threading.Timer(2.0, lambda: os._exit(0)).start()
            return

        if u.path == "/api/run":
            with _LOCK:
                if STATE["running"]:
                    _json(self, {"ok": False, "msg": "已经在跑了"}, 409)
                    return
                STATE.update(running=True, stage="准备", pct=1, msg="启动采集",
                             last_result=None)
            threading.Thread(target=_run_async, daemon=True).start()
            _json(self, {"ok": True})
            return

        if u.path == "/api/xhs/collect":
            import social
            panel = (payload.get("panel") or "").strip().lower()
            note_cap = payload.get("notes_per_keyword")
            kw_limit = payload.get("max_keywords")

            def worker():
                plan = social.keyword_plan()
                if panel in ("zhipu", "tangjie", "competitor"):
                    plan = [x for x in plan if x["panel"] == panel]
                if kw_limit:
                    try:
                        plan = plan[:max(1, min(int(kw_limit), 20))]
                    except Exception:
                        pass
                return social.collect(limit_per_kw=note_cap, keywords=plan or None)
            tk = _bg("xhs", worker)
            _json(self, {"ok": True, "task": tk["status"]})
            return

        if u.path == "/api/xhs/restart":
            import xhs_sidecar
            _json(self, {"ok": True, "engine": xhs_sidecar.restart()})
            return

        if u.path == "/api/xhs/mode":
            mode = (payload.get("mode") or "").strip()
            if mode not in ("off", "local", "vendor"):
                _json(self, {"ok": False, "msg": "mode 只支持 off/local/vendor"}, 400)
                return
            cfg = pipeline.load_config()
            cfg.setdefault("channels", {})["xiaohongshu_mode"] = mode
            pipeline.save_config(cfg)
            if mode == "local":
                import xhs_sidecar
                _json(self, {"ok": True, "mode": mode, "engine": xhs_sidecar.start()})
                return
            # 切走就收掉内置进程，不常驻
            import xhs_sidecar
            xhs_sidecar.stop()
            _json(self, {"ok": True, "mode": mode})
            return

        if u.path == "/api/weibo/qr/poll":
            import weibo as weibo_mod
            _json(self, weibo_mod.qr_poll((payload.get("qrid") or "").strip()))
            return

        if u.path == "/api/weibo/mode":
            mode = (payload.get("mode") or "").strip()
            if mode not in ("off", "local"):
                _json(self, {"ok": False, "msg": "mode 只支持 off/local"}, 400)
                return
            cfg = pipeline.load_config()
            cfg.setdefault("channels", {})["weibo_mode"] = mode
            pipeline.save_config(cfg)
            if mode == "off":
                import weibo as weibo_mod
                weibo_mod.clear_cookies()
            _json(self, {"ok": True, "mode": mode})
            return

        if u.path == "/api/weibo/logout":
            import weibo as weibo_mod
            weibo_mod.clear_cookies()
            _json(self, {"ok": True})
            return

        if u.path == "/api/config":
            cfg = pipeline.load_config()
            incoming = payload.get("llm") or {}
            cur = cfg.setdefault("llm", {})
            for k in ("base_url", "model"):
                if incoming.get(k):
                    cur[k] = incoming[k].strip()
            key = (incoming.get("api_key") or "").strip()
            if key and not key.startswith("••••"):
                cur["api_key"] = key
            if incoming.get("clear_key"):
                cur["api_key"] = ""
            sch_in = payload.get("schedule")
            if isinstance(sch_in, dict):
                sch = cfg.setdefault("schedule", {})
                for k in ("morning", "evening"):
                    v = str(sch_in.get(k) or "").strip()
                    if len(v) == 5 and v[2] == ":":
                        hh, mm = v[:2], v[3:]
                        if hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60:
                            sch[k] = f"{int(hh):02d}:{int(mm):02d}"
                if "token_budget_daily" in sch_in:
                    try:
                        sch["token_budget_daily"] = max(0, int(sch_in["token_budget_daily"]))
                    except Exception:
                        pass
                if "evening_guard" in sch_in:
                    try:
                        g = float(sch_in["evening_guard"])
                        sch["evening_guard"] = min(1.0, max(0.1, g))
                    except Exception:
                        pass
            rate_in = payload.get("llm_rate")
            if isinstance(rate_in, dict):
                r = {}
                try:
                    r["max_concurrent"] = max(1, min(8, int(rate_in.get("max_concurrent") or 2)))
                except Exception:
                    r["max_concurrent"] = 2
                try:
                    r["min_interval_ms"] = max(0, min(5000, int(rate_in.get("min_interval_ms") or 450)))
                except Exception:
                    r["min_interval_ms"] = 450
                cfg["llm_rate"] = r
            if isinstance(payload.get("llm_profiles"), list):
                old = {str(p.get("id")): p for p in (cfg.get("llm_profiles") or [])}
                pros = []
                for i, pr in enumerate(payload["llm_profiles"][:12]):
                    if not isinstance(pr, dict):
                        continue
                    pid = str(pr.get("id") or f"p{i}")[:32]
                    prev = old.get(pid) or {}
                    raw_key = str(pr.get("api_key") or "").strip()[:400]
                    # 前端回传的是「••••1234」或空：沿用已存 Key，绝不当成新 Key 落盘
                    if not raw_key or raw_key.startswith("••••"):
                        key_val = prev.get("api_key") or ""
                        in_kc = bool(prev.get("api_key_in_keychain"))
                    else:
                        key_val, in_kc = raw_key, False
                    pros.append({
                        "id": pid,
                        "name": str(pr.get("name") or f"档案{i+1}")[:40],
                        "base_url": str(pr.get("base_url") or "").strip()[:300],
                        "model": str(pr.get("model") or "").strip()[:120],
                        "api_key": key_val,
                        "api_key_in_keychain": in_kc,
                        "web_search": bool(pr.get("web_search", True)),
                    })
                cfg["llm_profiles"] = pros
            if payload.get("llm_active") is not None:
                cfg["llm_active"] = str(payload["llm_active"])[:32]
            if "llm_profiles" in payload or "llm_active" in payload:
                cfg = pipeline.apply_active_profile(cfg)
            if incoming.get("clear_key"):
                # 清空当前激活档案的 Key（并让 Keychain 里的旧值一并删除）
                cfg.setdefault("llm", {})["api_key"] = ""
                act = cfg.get("llm_active")
                for pr in cfg.get("llm_profiles") or []:
                    if pr.get("id") == act:
                        pr["api_key"] = ""
                        pr["api_key_in_keychain"] = False
            ch_in = payload.get("channels") or {}
            if "wechat_articles" in ch_in:
                links = ch_in["wechat_articles"]
                if isinstance(links, str):
                    links = [x.strip() for x in links.splitlines() if x.strip()]
                ch2 = cfg.setdefault("channels", {})
                ch2["wechat_articles"] = [u for u in links if "mp.weixin.qq.com" in str(u)][:50]
                ch2["articles_seeded"] = True
            if "wechat_feeds" in ch_in:
                feeds = ch_in["wechat_feeds"]
                if isinstance(feeds, str):
                    feeds = [x.strip() for x in feeds.splitlines() if x.strip()]
                ch = cfg.setdefault("channels", {})
                ch["wechat_feeds"] = [u for u in feeds if str(u).startswith("http")][:50]
                ch["presets_seeded"] = True
            if "xiaohongshu_mode" in ch_in:
                cfg.setdefault("channels", {})["xiaohongshu_mode"] = ch_in["xiaohongshu_mode"]
            if isinstance(ch_in.get("xiaohongshu_panels"), dict):
                panels = {}
                for p in ("zhipu", "tangjie", "competitor"):
                    vals = ch_in["xiaohongshu_panels"].get(p)
                    if isinstance(vals, str):
                        vals = [x.strip() for x in vals.replace("，", ",").split(",")]
                    if isinstance(vals, list):
                        panels[p] = [str(x).strip()[:40] for x in vals if str(x).strip()][:30]
                if panels:
                    cfg.setdefault("channels", {})["xiaohongshu_panels"] = panels
            for k, lo, hi in (("xiaohongshu_max_keywords", 1, 30),
                              ("xiaohongshu_notes_per_keyword", 1, 20),
                              ("xiaohongshu_comments_per_note", 1, 100),
                              ("xiaohongshu_daily_max_keywords", 1, 12),
                              ("xiaohongshu_daily_notes_per_keyword", 1, 10)):
                if k in ch_in:
                    try:
                        cfg.setdefault("channels", {})[k] = max(lo, min(hi, int(ch_in[k])))
                    except Exception:
                        pass
            if "xiaohongshu_daily" in ch_in:
                cfg.setdefault("channels", {})["xiaohongshu_daily"] = bool(
                    ch_in["xiaohongshu_daily"])
            if "onboarded" in payload:
                cfg["onboarded"] = bool(payload["onboarded"])
            pipeline.save_config(cfg)
            _json(self, {"ok": True})
            return

        if u.path == "/api/chat":
            q = (payload.get("q") or "").strip()
            try:
                ans = chat_mod.answer(q)
            except Exception as e:
                ans = {"text": f"出错了：{str(e)[:120]}", "cites": []}
            _json(self, ans)
            return

        if u.path == "/api/predict":
            import predict
            action = payload.get("action")
            if action == "commit":
                n = predict.commit(payload.get("items") or [])
                _json(self, {"ok": True, "committed": n})
            elif action == "settle":
                _json(self, {"ok": True, "settled": predict.settle_due()})
            elif action in ("propose", "propose_llm"):
                ent = (payload.get("entity") or "").strip() or None
                props = (predict.propose_llm(ent) if action == "propose_llm"
                         else predict.propose(ent, use_llm=bool(payload.get("llm"))))
                _json(self, {"ok": True, "proposals": props})
            else:
                _json(self, {"ok": False, "msg": "unknown action"}, 400)
            return

        if u.path == "/api/daily-report":
            raw_date = payload.get("date")
            if raw_date is not None and not isinstance(raw_date, str):
                _json(self, {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}, 400)
                return
            requested = (raw_date or "").strip()
            if requested and not _valid_report_date(requested):
                _json(self, {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}, 400)
                return
            try:
                result = _do_daily_report(requested or None, force=bool(payload.get("force", True)))
                _json(self, {"ok": True, **result})
            except ValueError as e:
                _json(self, {"ok": False, "error": str(e)[:300]}, 400)
            except RuntimeError as e:
                code = 409 if "采集正在运行" in str(e) else 500
                _json(self, {"ok": False, "error": str(e)[:300]}, code)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)[:300]}, 500)
            return

        if u.path == "/api/task":
            kind = payload.get("type") or "collect"
            TASKS.append({"type": kind, "label": payload.get("label") or KIND_LABEL.get(kind, kind),
                          "status": "queued", "at": db.now_iso(), "meta": payload.get("args") or {}})
            if kind == "collect" and not STATE["running"]:
                with _LOCK:
                    STATE.update(running=True, stage="准备", pct=1, msg="启动采集", last_result=None)
                TASKS[-1]["status"] = "running"
                threading.Thread(target=_run_async, daemon=True).start()
            elif kind == "dedupe":
                TASKS.pop()
                _bg("dedupe", _do_dedupe)
            elif kind == "weekly":
                TASKS.pop()
                _bg("weekly", lambda: _do_weekly((payload.get("args") or {}).get("week")))
            elif kind == "daily_report":
                TASKS.pop()
                args = payload.get("args") or {}
                _bg("daily_report", lambda: _do_daily_report(
                    args.get("date"), force=bool(args.get("force", True))))
            else:
                TASKS[-1]["status"] = "done"
            _json(self, {"ok": True})
            return

        if u.path == "/api/llm-models":
            base = (payload.get("base_url") or "").rstrip("/")
            key = payload.get("api_key") or ""
            if not base or not key:
                _json(self, {"ok": False, "msg": "需要先填 API 地址和 Key"})
                return
            import urllib.request as _u
            ids, err = [], None
            for path in ("/models", "/v1/models"):
                try:
                    req = _u.Request(base + path, headers={
                        "Authorization": "Bearer " + key, "Accept": "application/json",
                        "User-Agent": "LLMIntelAgent/1.0"})
                    with _u.urlopen(req, timeout=15) as r:
                        j = json.loads(r.read().decode("utf-8", "ignore"))
                    for m in (j.get("data") or j.get("models") or []):
                        mid = m.get("id") or m.get("name") if isinstance(m, dict) else str(m)
                        if mid:
                            ids.append(str(mid))
                    if ids:
                        break
                except Exception as e:
                    err = str(e)[:120]
            if ids:
                _json(self, {"ok": True, "models": sorted(set(ids))[:200]})
            else:
                _json(self, {"ok": False, "msg": f"拉不到模型列表（{err or '该服务不支持 /models'}）；可手填模型名"})
            return

        if u.path == "/api/test-llm":
            cfg = pipeline.load_config()
            llm = LLM(cfg.get("llm") or {})
            if not llm.enabled:
                _json(self, {"ok": False, "msg": "尚未配置 API Key"})
                return
            ok, msg = llm.test()
            _json(self, {"ok": ok, "msg": msg})
            return

        self.send_error(404)


def serve(port: int = 8765) -> ThreadingHTTPServer:
    db.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv
