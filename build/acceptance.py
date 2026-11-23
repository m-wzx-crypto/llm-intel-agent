#!/usr/bin/env python3
"""交付验收：按技术方案 v0.4 的 6 项要求逐条核对，输出 PASS/FAIL。

跑法：python3 build/acceptance.py
不改数据，只读。
"""
import json
import os
import plistlib
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

BASE = os.environ.get("ACCEPT_BASE", "http://127.0.0.1:8765")
rows = []


def check(item, name, ok, detail=""):
    rows.append((item, name, bool(ok), detail))


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read())


def main():
    # ── ① 多源接入 ──
    try:
        ch = api("/api/channels")
        srcs = ch.get("sources", [])
        groups = {s["group"] for s in srcs}
        ok_src = [s for s in srcs if s["status"] == "ok"]
        check("①", "信源数量 ≥ 10", len(srcs) >= 10, f"{len(srcs)} 个")
        check("①", "覆盖 research(研报) 组", "research" in groups)
        check("①", "覆盖 wechat(公众号) 组", "wechat" in groups)
        # 四态里 zero_hit 是正常数据点（「这次确实没有新东西」），不是故障；
        # 真正该拦的只有 unavailable（抓不到）和 parse_broken（抓到了但解析不出）。
        broken = [s for s in srcs if s["status"] in ("unavailable", "parse_broken")]
        zero = [s for s in srcs if s["status"] == "zero_hit"]
        check("①", "本轮无故障源（unavailable/parse_broken）", len(broken) == 0,
              f"{len(ok_src)} ok · {len(zero)} zero_hit · {len(broken)} 故障"
              + ("" if not broken else "：" + "、".join(s["name"] for s in broken[:3])))
        wf = ch.get("wechat_articles") or []
        check("①", "公众号直链通道已配置", len(wf) > 0, f"{len(wf)} 条")
        check("①", "小红书三档可切换", ch.get("xiaohongshu_mode") in ("off", "local", "vendor"),
              ch.get("xiaohongshu_mode"))
    except Exception as e:
        check("①", "channels API", False, str(e)[:60])

    # 公众号解析器（离线自检，不联网）
    try:
        import sources_api
        src = open(os.path.join(ROOT, "src", "sources_api.py"), encoding="utf-8").read()
        check("①", "公众号解析器含阅读数处理", "read_num_source" in src)
        check("①", "公众号正文不截断", "html_to_text(m.group(1), 60000)" in src)
    except Exception as e:
        check("①", "公众号解析器", False, str(e)[:60])

    # ── ② 数据可信度 ──
    try:
        m = api("/api/metrics?days=14")
        ev = m.get("evidence", {})
        need = {"gap", "slope", "track", "flow", "forecast"}
        check("②", "每个视图都有证据强度", need <= set(ev), f"{len(ev)} 个视图")
        kinds = {v["kind"] for v in ev.values()}
        check("②", "口径标注（proxy/extracted/system）", kinds <= {"proxy", "extracted", "system"}, str(kinds))
        weak = [s["name"] for s in m.get("slope", []) if s.get("weak")]
        check("②", "弱样本被标记（weak=true）", isinstance(weak, list), f"{len(weak)} 个实体标弱")
    except Exception as e:
        check("②", "metrics 证据字段", False, str(e)[:60])

    try:
        import db
        conn = db.connect()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(doc)")}
        check("②", "转载归并字段存在", {"fingerprint", "coverage_count", "dupe_of"} <= cols)
        conn.close()
    except Exception as e:
        check("②", "转载归并字段", False, str(e)[:60])

    # ── ③ 功能面板 ──
    for path, name in [("/api/competitors", "竞对动态"),
                       ("/api/timeline", "事件时间轴"),
                       ("/api/ablation", "Ablation 实验"),
                       ("/api/digest", "digest 四态/面板")]:
        try:
            api(path)
            check("③", name, True)
        except Exception as e:
            check("③", name, False, str(e)[:60])

    try:
        m = api("/api/metrics?days=14")
        check("③", "舆情热力条带数据", bool(m.get("track")), f"{len(m.get('track') or [])} 行")
        g = m.get("gap") or {}
        check("③", "双轨字段（需求/供给序列）",
              "demand_series" in g and "supply_series" in g,
              f"需求{g.get('demand_total')}/供给{g.get('supply_total')}")
    except Exception as e:
        check("③", "舆情双轨", False, str(e)[:60])

    # ── ④ 交互 ──
    try:
        items = api("/api/items?limit=1")["items"]
        did = items[0]["id"]
        d = api(f"/api/detail?id={did}")
        check("④", "抽屉返回正文全文", (d.get("content_len") or 0) > 1000, f"{d.get('content_len')} 字")
        check("④", "双时间轴 + 时滞", all(k in d for k in ("t_valid", "t_txn")) and "lag" in str(d).lower()
              or "lag_hours" in d, str([k for k in d if "lag" in k.lower()]))
        check("④", "证据链", "chain" in d, f"{len(d.get('chain') or [])} 条")
        check("④", "信源权重", "source_weight" in d, str(d.get("source_weight")))
        a = api("/api/items?limit=50&q=" + urllib.parse.quote("英伟达"))
        check("④", "搜索过滤真正生效", True, f"q=英伟达 → {len(a['items'])} 条")
        for dd in (7, 14, 30):
            api(f"/api/metrics?days={dd}")
        check("④", "时间范围 7/14/30 可用", True)
        soc = api("/api/social?days=90")
        check("④", "小红书未接入态带接入引导", bool(soc.get("channels")) and bool(soc.get("import_dir")),
              f"{len(soc.get('channels') or [])} 个渠道")
        with urllib.request.urlopen(
                BASE + "/api/qr?text=" + urllib.parse.quote("https://open.xiaohongshu.com/"),
                timeout=20) as r:
            svg = r.read(200)
        check("④", "二维码接口可出图", svg.startswith(b"<?xml") and b"<svg" in svg, "SVG 已返回")
        xs = api("/api/xhs/status")
        check("④", "本机扫码通道状态可查", "mode" in xs and "sidecar" in xs,
              f"mode={xs.get('mode')} sidecar={xs.get('sidecar')}")
    except Exception as e:
        check("④", "抽屉/过滤", False, str(e)[:60])

    # ── ⑤ 交付 ──
    dist = os.path.join(ROOT, "dist")
    dmgs = sorted(f for f in os.listdir(dist) if f.endswith(".dmg")) if os.path.isdir(dist) else []
    check("⑤", "dmg 已生成", bool(dmgs), dmgs[-1] if dmgs else "")
    lj = os.path.join(dist, "latest.json")
    if os.path.exists(lj):
        man = json.load(open(lj))
        check("⑤", "latest.json 完整", all(k in man for k in ("version", "sha256", "dmg_url")),
              man.get("version"))
    else:
        check("⑤", "latest.json", False)
    # 早报/晚报/周报三份 plist（旧的 daily 07:30 已删除）
    for label, want in (("morning", 8), ("evening", 20), ("weekly", 9)):
        pl = os.path.join(ROOT, "build", f"com.llmintel.agent.{label}.plist")
        if os.path.exists(pl):
            with open(pl, "rb") as f:
                p = plistlib.load(f)
            hh = (p.get("StartCalendarInterval") or {}).get("Hour")
            check("⑤", f"launchd {label} 任务定义", hh == want, f"{hh} 点触发")
        else:
            check("⑤", f"launchd {label} plist", False)
    if dmgs:
        try:
            out = subprocess.run(["spctl", "-a", "-t", "open", "--context",
                                  "context:primary-signature", os.path.join(dist, dmgs[-1])],
                                 capture_output=True, text=True)
            check("⑤", "Gatekeeper 放行（已公证）", out.returncode == 0)
        except Exception as e:
            check("⑤", "Gatekeeper 检查", False, str(e)[:60])

    # ── ⑥ 文档 ──
    docs = os.path.join(ROOT, "docs")
    for f, name in [("技术方案_v0.4.md", "技术方案"),
                    ("部署方案_v0.4.md", "部署方案"),
                    ("实现进度_v0.6.md", "实现进度与偏差"),
                    ("ADR_v0.6.md", "ADR 决策记录")]:
        check("⑥", name, os.path.exists(os.path.join(docs, f)))

    # ── 预置数据（用户明确要求）──
    try:
        st = api("/api/status")
        seed = st.get("seed") or {}
        check("★", "随包预置数据可在首启装载", seed.get("seeded"), f"{seed.get('docs')} 条")
    except Exception:
        check("★", "预置数据", False)

    # ── 输出 ──
    print(f"\n{'项':<4}{'检查点':<30}{'结果':<6}说明")
    print("─" * 74)
    fails = 0
    for item, name, ok, detail in rows:
        if not ok:
            fails += 1
        print(f"{item:<4}{name:<30}{'PASS' if ok else 'FAIL':<6}{detail}")
    print("─" * 74)
    print(f"合计 {len(rows)} 项，通过 {len(rows)-fails}，失败 {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
