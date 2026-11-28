"""流水线编排：感知 → 抽取 → 建图(落库) → 快照 → 简报

对应技术方案 §1 的 DAG，本版实现的区间是 T0+00 ~ T0+50（感知/抽取/融合/建图）。
检测层（Hawkes/缺口/异常）与预测结算留接口，不在本版跑。
"""
import hashlib
import re

import db
import collector
from collector import fetch_article, fetch_source
import sources_api
from extract import LLM, classify, rule_summary
import sources as SRC
import wechat_presets
import wechat_guard

MAX_ARTICLE_FETCH = 40   # 单次最多抓多少篇正文（控成本）

def _clean(s: str) -> str:
    """去掉标点/空白/符号/控制字符，只留文字与数字（用于转载判定）"""
    import unicodedata as ud
    return "".join(ch for ch in (s or "")
                   if ud.category(ch)[0] not in ("P", "Z", "S", "C"))


def norm_title(s: str) -> str:
    """标题归一化：去标点/空格/来源前缀，用于识别「同一篇被多家转载」"""
    s = (s or "").strip()
    for pre in ("独家丨", "独家|", "快讯：", "快讯|", "观点｜", "译文｜", "原文："):
        if s.startswith(pre):
            s = s[len(pre):]
    return _clean(s).lower()[:60]


def fingerprint(title: str, content: str) -> str:
    """标题指纹为主，正文前 120 字做补充（防标题党改字）"""
    body = _clean((content or "")[:120])[:40]
    return f"{norm_title(title)}|{body}"
MAX_LLM_SUMMARY = 60     # 单次最多调多少次 LLM


def _guid_hash(url: str, title: str) -> str:
    return hashlib.sha256(f"{url}::{title}".encode()).hexdigest()[:32]


def _exists(conn, guid: str) -> bool:
    return conn.execute("SELECT 1 FROM doc WHERE guid=?", (guid,)).fetchone() is not None


def _generate_daily_report(run_id: int, snapshot_id: int, llm, progress=None) -> dict:
    """晨间报告与采集解耦：失败只记结果，不把已成功的采集改判为失败。"""
    import daily_report
    before = (getattr(llm, "tokens", 0), getattr(llm, "cached_tokens", 0),
              getattr(llm, "reasoning_tokens", 0))
    result = {"generated": False, "tokens": 0}
    try:
        if progress:
            progress("分析", 93, "生成每日自主分析报告")
        report = daily_report.ensure_report(run_id=run_id, snapshot_id=snapshot_id,
                                            llm=llm, force=True)
        result.update(generated=True, report_date=report["report_date"],
                      mode=report["mode"], tokens=report["tokens"])
    except Exception as e:
        result["error"] = str(e)[:300]
    finally:
        used = getattr(llm, "tokens", 0) - before[0]
        cached = getattr(llm, "cached_tokens", 0) - before[1]
        reasoning = getattr(llm, "reasoning_tokens", 0) - before[2]
        result["tokens"] = max(0, used)
        if used > 0:
            try:
                db.add_tokens(used, "daily_report", cached, reasoning)
            except Exception:
                pass
    return result


def run_daily(progress=None, slot: str = "morning") -> dict:
    """跑一次完整采集（跨进程串行：拿不到 data.lock 就直接跳过）。"""
    import locks
    handle = locks.acquire("data", blocking=False)
    if handle is None:
        return {"skipped": "locked",
                "reason": "另一个采集任务正在运行（早报/晚报/GUI 互斥）",
                "slot": slot}
    try:
        return _run_daily_impl(progress=progress, slot=slot)
    finally:
        locks.release(handle)


def _run_daily_impl(progress=None, slot: str = "morning") -> dict:
    """跑一次完整采集。progress 是可选回调 (stage, pct, msg)"""
    def say(stage, pct, msg):
        if progress:
            try:
                progress(stage, pct, msg)
            except Exception:
                pass

    db.init_db()
    cfg = load_config()
    # 公众号 RSS：用户在设置里配了才并入
    feeds = (cfg.get("channels") or {}).get("wechat_feeds") or []
    wx_urls = (cfg.get("channels") or {}).get("wechat_articles") or []
    if wx_urls:
        # 已入库的直链不再重抓：每篇几 MB，天天重抓纯浪费；全部抓过就不挂这个源
        try:
            _c0 = db.connect()
            try:
                _known = {r[0] for r in _c0.execute(
                    "SELECT url FROM doc WHERE url LIKE 'https://mp.weixin.qq.com/%'")}
            finally:
                _c0.close()
            wx_urls = [u for u in wx_urls if u not in _known]
        except Exception:
            pass
    all_sources = list(SRC.SOURCES)
    for i, u in enumerate(feeds):
        u = (u or "").strip()
        if u.startswith("http"):
                all_sources.append({"id": f"wx{i}",
                                "name": wechat_presets.name_of(u) or f"公众号{i+1}",
                                "url": u, "group": "wechat", "tier": 1})
    # 小红书 vendor 模式：从商业数据源导入的数据并入采集
    try:
        import social
        if social.get_mode() == "vendor":
            st = social.probe()
            all_sources.append({"id": "xhs_vendor", "name": "小红书(vendor)", "kind": "api",
                                "url": "xhs:vendor", "group": "social", "tier": 2})
            _xhs_status = st
        else:
            _xhs_status = social.probe()
    except Exception:
        _xhs_status, st = None, None

    # 微博本机只读通道：扫码登录后并入采集（未登录不占用探测位）
    try:
        import weibo as weibo_mod
        if weibo_mod.get_mode() == "local" and weibo_mod.login_status().get("logged_in"):
            all_sources.append({"id": "weibo_local", "name": "微博", "kind": "api",
                                "url": "weibo:local", "group": "social", "tier": 2})
    except Exception:
        pass

    wx_block = wechat_guard.cooldown_status() if wx_urls else {"active": False}
    if wx_urls and not wx_block["active"]:
        all_sources.append({"id": "wechat_direct", "name": "公众号直链", "kind": "api",
                            "url": "wechat:urls", "group": "wechat", "tier": 1})
    llm = LLM(cfg.get("llm") or {})
    # 预算已用完就别再调模型了——采集照跑（数据要进来），摘要走规则
    try:
        import ratelimit
        ratelimit.check_budget()
    except Exception as e:
        llm.budget_hit = True
        say("预算", 3, f"模型预算已用完，本次全程用规则摘要（{str(e)[:40]}）")
    run_id = db.start_run()
    conn = db.connect()

    ok = bad = new_items = 0
    fetched_bodies = 0
    llm_calls = 0
    per_source = []
    if wx_urls and wx_block.get("active"):
        msg = (f"公众号限流冷却中，预计 {wx_block.get('until_text')} 恢复"
               f"（剩余 {wx_block.get('remaining_h')}h）；本轮跳过直链")
        db.log_probe(run_id, "wechat_direct", "公众号直链", "rate_limited",
                     hit_count=0, http_status=None, latency_ms=0, error=msg)
        per_source.append({"id": "wechat_direct", "name": "公众号直链",
                           "status": "rate_limited", "hits": 0, "error": msg})
        bad += 1

    try:
        say("感知", 5, f"开始采集 {len(all_sources)} 个信源")
        fresh = []  # (src, entry, t_valid)
        for i, src in enumerate(all_sources):
            say("感知", 5 + int(35 * i / max(1, len(all_sources))), f"抓取 {src['name']}")
            if src.get("kind") == "api":
                try:
                    if src["url"] == "sec:filings":
                        entries = sources_api.fetch_sec_filings(days=7)
                    elif src["url"] == "goldman:insights":
                        entries = sources_api.fetch_goldman_insights(days=10, limit=10)
                    elif src["url"] == "blackrock:cn":
                        entries = sources_api.fetch_blackrock_cn()
                    elif src["url"] == "xhs:vendor":
                        import social
                        entries = social.fetch_vendor()
                    elif src["url"] == "weibo:local":
                        import weibo as weibo_mod
                        entries = weibo_mod.fetch_login()
                    elif src["url"] == "baidu:news:zhipu":
                        entries = sources_api.fetch_baidu_news(
                            "智谱", pages=1, days=120,
                            must_any=("智谱", "GLM", "ChatGLM", "Zhipu"))
                    elif src["url"] == "baidu:news:tangjie":
                        entries = sources_api.fetch_baidu_news(
                            "唐杰 智谱", pages=1, days=365,
                            must_any=("唐杰", "智谱", "GLM"),
                            exclude=("唐杰雄",))
                    elif src["url"] == "baidu:hot":
                        entries = sources_api.fetch_baidu_hot(limit=20)
                    elif src["url"] == "wechat:urls":
                        entries = sources_api.fetch_wechat_urls(wx_urls)
                    elif src["url"] == "eastmoney:report":
                        entries = sources_api.fetch_reports(days=3)
                    elif src["url"] == "cninfo:notice":
                        entries = sources_api.fetch_announcements(days=2)
                    elif src["url"] == "sina:7x24":
                        entries = sources_api.fetch_sina_7x24(pages=3)
                    elif src["url"] == "em:kuaixun":
                        entries = sources_api.fetch_em_kuaixun(pages=3)
                    elif src["url"] == "ths:7x24":
                        entries = sources_api.fetch_ths_7x24(pages=4)
                    elif src["url"] == "wscn:lives":
                        entries = sources_api.fetch_wscn_lives(pages=2)
                    elif src["url"] == "em:ann":
                        entries = sources_api.fetch_em_announcements(pages=12, max_body=30)
                    elif src["url"] == "sec:deals":
                        entries = sources_api.fetch_sec_deals(days=3, per_query=2, per_cik=1)
                    elif src["url"] == "em:search:zhipu":
                        entries = sources_api.fetch_news_search(
                            "智谱", pages=2, page_size=50, days=30,
                            must_any=("智谱", "GLM", "ChatGLM", "Zhipu"))
                    elif src["url"] == "em:search:tangjie":
                        entries = sources_api.fetch_news_search(
                            "唐杰", pages=3, page_size=50, days=180,
                            must_all=("唐杰",),
                            must_any=("智谱", "GLM", "清华", "大模型", "人工智能", "AI",
                                      "教授", "智源", "论文", "KDD", "面壁"),
                            must_title_any=("唐杰", "智谱", "清华", "大模型", "人工智能"),
                            exclude=("唐杰雄",))
                    elif src["url"] == "em:search:tangjie_wide":
                        entries = sources_api.fetch_news_search(
                            "唐杰 智谱", pages=2, page_size=50, days=365,
                            must_all=("唐杰",),
                            must_any=("智谱", "GLM", "ChatGLM", "Zhipu"),
                            exclude=("唐杰雄",))
                    elif src["url"] == "zhipu:news":
                        entries = sources_api.fetch_zhipu_news(limit=60, days=1500)
                    else:
                        entries = []
                    res = {"status": "ok" if entries else "zero_hit", "entries": entries,
                           "http_status": 200, "latency_ms": 0, "error": None}
                except wechat_guard.WechatRateLimited as e:
                    hours = float((cfg.get("channels") or {}).get("wechat_cooldown_hours")
                                  or wechat_guard.DEFAULT_HOURS)
                    st = wechat_guard.set_cooldown(hours, str(e))
                    msg = (f"公众号访问受限（已抓 {e.fetched} 篇），暂停直链至 "
                           f"{st.get('until_text')}（{hours:g}h）")
                    res = {"status": "rate_limited", "entries": e.entries or [],
                           "http_status": 429, "latency_ms": 0, "error": msg}
                except Exception as e:
                    res = {"status": "unavailable", "entries": [], "http_status": None,
                           "latency_ms": 0, "error": str(e)[:160]}
            else:
                res = fetch_source(src)
                # 自定义 feed 用频道名当源名；预置号已经有名字，不动
                if (src.get("group") == "wechat" and res.get("feed_title")
                        and (src.get("name") or "").startswith("公众号")):
                    src["name"] = res["feed_title"][:40]
                if src.get("filter") == "ai_deal":
                    res["entries"] = [
                        e for e in res["entries"]
                        if sources_api.focus_text(f"{e.get('title','')} {e.get('desc','')}")
                    ]
                    if not res["entries"]:
                        res["status"] = "zero_hit"
            db.log_probe(run_id, src["id"], src["name"], res["status"],
                         hit_count=len(res["entries"]), http_status=res["http_status"],
                         latency_ms=res["latency_ms"], error=res["error"])
            per_source.append({"id": src["id"], "name": src["name"], "status": res["status"],
                               "hits": len(res["entries"]), "error": res["error"]})
            if res["status"] in ("ok", "zero_hit"):
                ok += 1
            else:
                bad += 1
            for e in res["entries"]:
                g = _guid_hash(e["link"], e["title"])
                if not _exists(conn, g):
                    fresh.append((src, e, g))

        # 小红书 local：并入每日采集（三面板关键词）。失败只记状态，不拖垮主流程。
        try:
            import social
            ch = cfg.get("channels") or {}
            if social.get_mode() == "local" and ch.get("xiaohongshu_daily", True):
                say("社媒", 38, "小红书本机采集（智谱/唐杰/竞对）")
                plan = social.keyword_plan()
                cap = int(ch.get("xiaohongshu_daily_max_keywords") or 6)
                xhs = social.collect(
                    limit_per_kw=int(ch.get("xiaohongshu_daily_notes_per_keyword") or 2),
                    keywords=plan[:max(1, min(cap, 12))],
                    detail_timeout=60,
                )
                if xhs.get("ok"):
                    xst, xerr = "ok", None
                elif xhs.get("need_login"):
                    xst, xerr = "need_login", xhs.get("reason")
                else:
                    xst, xerr = "unavailable", xhs.get("reason")
                hits = int(xhs.get("comments") or 0)
                db.log_probe(run_id, "xhs_local", "小红书(本机)", xst,
                             hit_count=hits, http_status=None, latency_ms=0,
                             error=xerr)
                per_source.append({"id": "xhs_local", "name": "小红书(本机)",
                                   "status": xst, "hits": hits, "error": xerr})
                if xst == "ok":
                    ok += 1
                else:
                    bad += 1
                _xhs_status = xhs
        except Exception as e:
            _xhs_status = {"ok": False, "reason": str(e)[:120]}

        # ① URL 级去重
        seen = set()
        stage1 = []
        for src, e, g in fresh:
            if g in seen:
                continue
            seen.add(g)
            stage1.append((src, e, g))

        # ② 转载归并：标题指纹相同 → 保留 t_valid 最早的一条，其余记 dupe_of
        by_fp, order = {}, []
        for src, e, g in stage1:
            fp = fingerprint(e["title"], e.get("desc", ""))
            e["_fp"] = fp
            if fp not in by_fp:
                by_fp[fp] = []
                order.append(fp)
            by_fp[fp].append((src, e, g))

        uniq, coverage, dups = [], {}, []
        for fp in order:
            group = by_fp[fp]
            group.sort(key=lambda x: (x[1].get("t_valid") or "", x[1].get("link") or ""))
            canonical = group[0]
            uniq.append(canonical)
            coverage[fp] = len(group)
            if len(group) > 1:
                dups.append((fp, group[1:]))

        merged = sum(1 for n in coverage.values() if n > 1)
        dup_group_map = {fp: group for fp, group in dups}

        say("抽取", 45, f"新条目 {len(uniq)} 条，抓正文")
        txn = db.now_iso()
        for idx, (src, e, g) in enumerate(uniq):
            body = e.get("_body") or ""
            if not body and not e.get("_no_fetch") and fetched_bodies < MAX_ARTICLE_FETCH:
                body = collector.clean_body(fetch_article(e["link"]), e["title"])
                fetched_bodies += 1
            if not body:
                body = e.get("desc") or ""

            cls = classify(e["title"], body)
            if llm.enabled and llm_calls < MAX_LLM_SUMMARY:
                summary = llm.summarize(e["title"], body)
                llm_calls += 1
            else:
                summary = rule_summary(e["title"], body)

            cov = coverage.get(e.get("_fp"), 1)
            cur = conn.execute(
                "INSERT INTO doc (guid,url,source_id,source_name,title,author,t_valid,t_txn,"
                "content,content_hash,fetched_ok,fingerprint,coverage_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (g, e["link"], src["id"], e.get("_source_name") or src["name"], e["title"], None,
                 e["t_valid"], txn, body,
                 hashlib.sha256(body.encode()).hexdigest()[:16], 1 if body else 0,
                 e.get("_fp"), cov))
            doc_id = cur.lastrowid
            if e.get("_read_num") is not None:
                conn.execute("UPDATE doc SET read_num=?, like_num=? WHERE id=?",
                             (e.get("_read_num"), e.get("_like_num"), doc_id))
            kind = "firsthand" if src["id"] == "zhipu_news" else "proxy"
            conn.execute(
                "INSERT INTO item (doc_id,category,score,keywords,summary,entities,created_at,"
                "evidence_n,evidence_kind) VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_id, cls["category"], cls["score"],
                 _j(cls["keywords"]), summary, _j(cls["entities"]), txn,
                 cov, kind))

            # 转载副本：只落 doc（供「哪几家转过」追溯），不再单独成条
            for dsrc, de, dg in dup_group_map.get(e.get("_fp"), []):
                conn.execute(
                    "INSERT OR IGNORE INTO doc (guid,url,source_id,source_name,title,author,"
                    "t_valid,t_txn,content,content_hash,fetched_ok,fingerprint,dupe_of)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dg, de["link"], dsrc["id"], de.get("_source_name") or dsrc["name"], de["title"], None,
                     de["t_valid"], txn, "", "", 0, e.get("_fp"), doc_id))
            new_items += 1
            if idx % 5 == 0:
                conn.commit()
                say("抽取", 45 + int(40 * idx / max(1, len(uniq))),
                    f"处理 {idx+1}/{len(uniq)}")
        conn.commit()

        # ③ 事件级去重合并：标题指纹只能抓「一字不差」，这里抓「换了个说法」。
        #    同一件事多家报道 → 收敛成一条，其余记 dupe_of，代表条带上全部来源。
        say("合并", 86, "归并同一事件的多家报道")
        dedupe_stats = {}
        try:
            import dedupe as dd
            dedupe_stats = dd.run(conn, days=10)
        except Exception as e:
            dedupe_stats = {"error": str(e)[:200]}

        say("建图", 90, "冻结快照")
        max_item = conn.execute("SELECT COALESCE(MAX(id),0) FROM item").fetchone()[0]
        snap = conn.execute("INSERT INTO snapshot (taken_at,max_item_id,run_id) VALUES (?,?,?)",
                            (db.now_iso(), max_item, run_id))
        snapshot_id = snap.lastrowid
        conn.commit()

        collection_tokens = llm.tokens
        collection_cached = llm.cached_tokens
        collection_reasoning = llm.reasoning_tokens
        db.finish_run(run_id, status="ok", new_items=new_items, ok=ok, bad=bad,
                      note=(f"slot={slot} fetched_bodies={fetched_bodies} "
                            f"llm_calls={llm_calls} merged={dedupe_stats.get('merged_docs', 0)}"),
                      cost=collection_tokens)
        # 记入当日 token 台账（早报/晚报/对话共用一份预算）
        try:
            db.add_tokens(collection_tokens, "run", collection_cached, collection_reasoning)
        except Exception:
            pass
        daily_report_result = None
        if slot == "morning":
            daily_report_result = _generate_daily_report(
                run_id, snapshot_id, llm, progress=progress)
        say("完成", 100, f"新增 {new_items} 条")
        return {"run_id": run_id, "new_items": new_items, "merged_stories": merged,
                "event_merge": dedupe_stats,
                "duplicates": sum(len(g) for g in dup_group_map.values()), "sources_ok": ok,
                "sources_bad": bad, "llm_calls": llm_calls, "tokens": collection_tokens,
                "daily_report": daily_report_result,
                "slot": slot, "today_tokens": db.today_tokens(),
                "xhs": _xhs_status, "sources": per_source}
    except Exception as e:
        conn.rollback()
        db.finish_run(run_id, status="error", new_items=new_items, ok=ok, bad=bad,
                      note=str(e)[:300])
        raise
    finally:
        conn.close()


def _j(v):
    import json
    return json.dumps(v, ensure_ascii=False)


DEFAULT_CONFIG = {
    # 本机密钥存储：macOS Keychain（不可用时自动退回 config）
    "security": {"key_store": "keychain"},
    "llm": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-flash",
        "web_search": True,
    },
    # 多套模型档案：随时切换，不用重填地址和 Key
    "llm_profiles": [
        {"id": "deepseek", "name": "DeepSeek 官方", "base_url": "https://api.deepseek.com",
         "model": "deepseek-flash", "api_key": "", "web_search": True},
        {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
         "model": "glm-5.3", "api_key": ""},
    ],
    "llm_active": "deepseek",
    # 首次配置向导是否走完：False 时前端才会自动弹（客户装好第一眼就要配 Key）
    "onboarded": False,
    "schedule": {
        "morning": "08:00",          # 早报：到点必跑
        "evening": "20:00",          # 晚报：到点跑，但要有余量
        # 每日 token 预算：按**全额**计（prompt 含缓存命中 + completion），不看缓存
        "token_budget_daily": 50_000_000,
        "evening_guard": 0.8,        # 已用超过预算的这个比例，就跳过晚报
    },
    # 出站限流：别一秒十几个并发打到模型网关或新闻站
    "llm_rate": {"max_concurrent": 2, "min_interval_ms": 450},
    "ui": {"window_days": 3, "per_panel": 8},
    "channels": {
        # 预置公众号（清单见 wechat_presets.py，均实测在更新）
        "wechat_feeds": wechat_presets.default_feeds(),
        # 预填两条实测可用的公众号文章（首次装好就能看到公众号通道在工作）
        # 预置直链：17 个重点号各一篇（智谱/DeepSeek/Kimi/MiniMax 官方 + 深度媒体）
        # 只抓首次，之后按 URL 跳过；账号清单见 docs/公众号接入清单_2026-11-23.md
        "wechat_articles": [
            "https://mp.weixin.qq.com/s/lgYAGMbTvinKerEEZmBpqA",
            "https://mp.weixin.qq.com/s/3nW9lvhHNEmsjmxUSPQTOg",
            "https://mp.weixin.qq.com/s/6qj9jKd9ElUISxk_nHKBGQ",
            "https://mp.weixin.qq.com/s/C8Rp8LkH5Y4LmhyO6IOU0A",
            "https://mp.weixin.qq.com/s/L9HrwgiDbKmSe2PpcSj6hw",
            "https://mp.weixin.qq.com/s/FMXvCJMwTyZPtuWDxCiTWQ",
            "https://mp.weixin.qq.com/s/7PPnkjE_S0mUHaUdhk2LIw",
            "https://mp.weixin.qq.com/s/JGMveAbTiBvPQhS934GCEg",
            "https://mp.weixin.qq.com/s/AZs2QLmb40b4yYgG03iqAQ",
            "https://mp.weixin.qq.com/s/0sE6BtG9KeZi_ny9gKMMLg",
            "https://mp.weixin.qq.com/s/qkhjjF4LQwtkgo1rnUkW9Q",
            "https://mp.weixin.qq.com/s/2xns9flD07aR-whJtPAvDA",
            "https://mp.weixin.qq.com/s/4EepqhILK0bO-dJWm5HMHw",
            "https://mp.weixin.qq.com/s/78KBHc4-4N5b2vfL9SkutQ",
            "https://mp.weixin.qq.com/s/O7RCVME1Kut-Z2oFYhgrkw",
            "https://mp.weixin.qq.com/s/yecfFcvwgARGHYgwOfYT3Q",
            "https://mp.weixin.qq.com/s/L81hPMQ66ddC260HwgePXw",
        ],
        "xiaohongshu_mode": "off",   # off | local | vendor
        "weibo_mode": "off",         # off | local（本机扫码登录后只读采集）
        "weibo_keywords": ["智谱", "GLM", "唐杰", "DeepSeek", "Kimi", "豆包"],
        "wechat_cooldown_hours": 6,  # 公众号直链被限流后暂停多久再试
        # 三面板关键词：竞对也纳入同一轮采集；顺序即优先级
        "xiaohongshu_panels": {
            "zhipu": ["智谱", "GLM", "ChatGLM"],
            "tangjie": ["唐杰", "张鹏"],
            "competitor": ["DeepSeek", "Kimi", "MiniMax", "Qwen", "豆包", "OpenAI",
                           "Gemini", "宇树", "智元", "英伟达"],
        },
        "xiaohongshu_max_keywords": 15,          # 手动采集单轮最多几个关键词（覆盖三面板全量）
        "xiaohongshu_notes_per_keyword": 4,      # 每个关键词读几条笔记
        "xiaohongshu_comments_per_note": 20,     # 每条笔记最多取多少评论
        "xiaohongshu_daily": True,               # local 模式是否并入每日采集
        "xiaohongshu_daily_max_keywords": 9,     # 定时任务的关键词上限（三面板各 3 个）
        "xiaohongshu_daily_notes_per_keyword": 2,
    },
    # 预填的关注面：客户装上就能看到这些实体的动静，不用从零配
    "watch": {
        "entities": ["智谱", "Gemini", "OpenAI", "Anthropic", "DeepSeek", "Qwen",
                     "豆包", "英伟达", "昇腾", "宇树", "智元", "银河通用"],
        "events": ["发布", "采购", "融资", "中标", "裁员", "断供"],
    },
}


# 旧版默认模型名 → 当前版本（老配置升级时自动换，用户自己改过的不动）
_MODEL_UPGRADE = {
    "deepseek-chat": "deepseek-v4.1-flash",
    "deepseek-reasoner": "deepseek-v4.1",
    "glm-4-plus": "glm-5.3",
    "glm-4-flash": "glm-5.3-flash",
    "glm-4": "glm-5.3",
}


def migrate_budget(cfg: dict) -> bool:
    """把旧默认预算 30 万 token 升到 50 万…不，升到 5 千万。

    只改「恰好等于旧默认值」的情况——用户自己填过的数字一律不动。
    旧默认值在当时是拍脑袋定的（按 300k 估的），客户现在的口径是每天不超 5 千万。
    """
    sch = cfg.get("schedule") or {}
    if int(sch.get("token_budget_daily") or 0) == 300_000:
        sch["token_budget_daily"] = 50_000_000
        cfg["schedule"] = sch
        return True
    return False


def migrate_wechat_presets(cfg: dict) -> bool:
    """老配置补上预置公众号；只补一次，用户后来点掉的不会被加回来。"""
    ch = cfg.setdefault("channels", {})
    if ch.get("presets_seeded"):
        return False
    feeds = [u for u in (ch.get("wechat_feeds") or []) if isinstance(u, str) and u.strip()]
    for u in wechat_presets.default_feeds():
        if u not in feeds:
            feeds.append(u)
    ch["wechat_feeds"] = feeds
    ch["presets_seeded"] = True
    try:
        save_config(cfg)
    except Exception:
        pass
    return True


def migrate_wechat_articles(cfg: dict) -> bool:
    """老配置补上预置的公众号直链；只补一次，用户删掉的不会被加回来。"""
    ch = cfg.setdefault("channels", {})
    if ch.get("articles_seeded"):
        return False
    arts = [u for u in (ch.get("wechat_articles") or []) if isinstance(u, str)]
    for u in DEFAULT_CONFIG["channels"]["wechat_articles"]:
        if u not in arts:
            arts.append(u)
    ch["wechat_articles"] = arts[:50]
    ch["articles_seeded"] = True
    try:
        save_config(cfg)
    except Exception:
        pass
    return True


def migrate_models(cfg: dict) -> bool:
    """把配置文件里过期的默认模型名换成当前版本，返回是否改动过"""
    changed = False
    for pr in cfg.get("llm_profiles") or []:
        m = pr.get("model")
        if m in _MODEL_UPGRADE:
            pr["model"] = _MODEL_UPGRADE[m]
            changed = True
    llm = cfg.get("llm") or {}
    if llm.get("model") in _MODEL_UPGRADE:
        llm["model"] = _MODEL_UPGRADE[llm["model"]]
        changed = True
    if changed:
        try:
            save_config(cfg)
        except Exception:
            pass
    return changed


def apply_active_profile(cfg: dict) -> dict:
    """把激活的档案写进 llm，让采集/对话直接用当前生效的那套"""
    profs = cfg.get("llm_profiles") or []
    act = cfg.get("llm_active")
    hit = next((p for p in profs if p.get("id") == act), None)
    if hit:
        cfg["llm"] = {"base_url": hit.get("base_url", ""),
                      "api_key": hit.get("api_key", ""),
                      "model": hit.get("model", ""),
                      "web_search": hit.get("web_search", True)}
    return cfg


def _key_store(cfg: dict) -> str:
    return str((cfg.get("security") or {}).get("key_store") or "keychain").lower()


def _hydrate_keys(cfg: dict) -> dict:
    """从 Keychain 取回 Key（配置文件里只留 api_key_in_keychain 标记）。"""
    try:
        import secrets_store
    except Exception:
        return cfg
    if _key_store(cfg) != "keychain" or not secrets_store.available():
        return cfg
    for pr in cfg.get("llm_profiles") or []:
        if pr.get("api_key_in_keychain"):
            k = secrets_store.get_key("profile:" + str(pr.get("id") or ""))
            if k:
                pr["api_key"] = k
    llm = cfg.get("llm") or {}
    if llm.get("api_key_in_keychain"):
        k = secrets_store.get_key("active")
        if k:
            llm["api_key"] = k
    # 激活档案有 Key 时始终以档案为准，避免两处不一致
    profs = cfg.get("llm_profiles") or []
    act = cfg.get("llm_active")
    hit = next((p for p in profs if p.get("id") == act), None)
    if hit and (hit.get("api_key") or "").strip():
        cfg["llm"] = {"base_url": hit.get("base_url", ""),
                      "api_key": hit.get("api_key", ""),
                      "model": hit.get("model", ""),
                      "web_search": hit.get("web_search", True)}
    return cfg


def _stash_keys(cfg: dict) -> dict:
    """把明文 Key 移进 Keychain；写成功后才清空配置字段。"""
    try:
        import secrets_store
    except Exception:
        return cfg
    if _key_store(cfg) != "keychain" or not secrets_store.available():
        return cfg

    def usable(k: str) -> bool:
        return bool(k) and not k.startswith("••••")

    for pr in cfg.get("llm_profiles") or []:
        k = (pr.get("api_key") or "").strip()
        account = "profile:" + str(pr.get("id") or "")
        if usable(k):
            if secrets_store.set_key(account, k):
                pr["api_key"] = ""
                pr["api_key_in_keychain"] = True
        elif pr.get("api_key_in_keychain"):
            secrets_store.delete_key(account)
            pr["api_key_in_keychain"] = False
    llm = cfg.get("llm") or {}
    k = (llm.get("api_key") or "").strip()
    if usable(k):
        if secrets_store.set_key("active", k):
            llm["api_key"] = ""
            llm["api_key_in_keychain"] = True
    elif llm.get("api_key_in_keychain"):
        secrets_store.delete_key("active")
        llm["api_key_in_keychain"] = False
    return cfg


def _has_plaintext_keys(cfg: dict) -> bool:
    for pr in cfg.get("llm_profiles") or []:
        k = (pr.get("api_key") or "").strip()
        if k and not k.startswith("••••"):
            return True
    k = ((cfg.get("llm") or {}).get("api_key") or "").strip()
    return bool(k and not k.startswith("••••"))


def load_config(skip_migrate: bool = False) -> dict:
    import json
    p = db.config_path()
    if not p or not __import__("os").path.exists(p):
        return dict(DEFAULT_CONFIG)
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        if not skip_migrate:
            try:
                migrate_models(merged)
                migrate_wechat_presets(merged)
                migrate_wechat_articles(merged)
                if migrate_budget(merged):
                    save_config(merged)
                # 一次性迁移：老配置里的明文 Key 写进 Keychain 后再清空
                if _has_plaintext_keys(merged) and _key_store(merged) == "keychain":
                    import secrets_store
                    if secrets_store.available():
                        _stash_keys(merged)
                        with open(p, "w", encoding="utf-8") as f:
                            json.dump(merged, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return _hydrate_keys(merged)
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    import json
    try:
        _stash_keys(cfg)
    except Exception:
        pass
    with open(db.config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # 当前进程继续用明文；磁盘上不留 Key
    try:
        _hydrate_keys(cfg)
    except Exception:
        pass
    try:
        import ratelimit
        ratelimit.invalidate()          # 预算/限流改了要立刻生效，不等缓存过期
    except Exception:
        pass
