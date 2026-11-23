#!/usr/bin/env python3
"""入口

  python main.py                 启动界面（默认浏览器打开）
  python main.py --run-daily     跑一次采集后退出（launchd 用这个）
  python main.py --port 8899     指定端口
  python main.py --no-open       不自动开浏览器
"""
import argparse
import json
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db          # noqa: E402
import pipeline    # noqa: E402


def _port_file() -> str:
    return os.path.join(db.app_dir(), "port.txt")


def saved_port() -> int:
    try:
        with open(_port_file()) as f:
            return int((f.read() or "").strip())
    except Exception:
        return 0


def save_port(port: int) -> None:
    try:
        with open(_port_file(), "w") as f:
            f.write(str(port))
    except Exception:
        pass


def probe_instance(port: int, timeout: float = 1.2) -> bool:
    """这个端口上是不是已经在跑我们的服务"""
    if not port:
        return False
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as r:
            return b"running" in r.read(600)
    except Exception:
        return False


DEFAULT_PORT = 8765      # 用户浏览器里那个标签页指向的地址，除非被占否则永远用它


def can_bind(port: int) -> bool:
    """能不能在本机回环上绑这个端口（能绑 = 旧进程已经彻底退出了）。

    必须带 SO_REUSEADDR：HTTP 服务本身是 allow_reuse_address=True 的，
    而 TIME_WAIT 里的连接会让"裸 bind"报占用——用裸 bind 探测会误判"端口还没释放"，
    于是白白漂到下一个端口。探测语义必须和真实 bind 一致。
    """
    import socket
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_port(port: int, seconds: float = 20.0) -> bool:
    """等端口释放。刚退出的进程可能还在 TIME_WAIT——不等就会漂到下一个端口，
    浏览器里的标签页随即指向死地址。"""
    for _ in range(int(seconds / 0.5)):
        if can_bind(port):
            return True
        time.sleep(0.5)
    return can_bind(port)


def find_free_port(preferred: int = 8765, tries: int = 20) -> int:
    import socket
    for p in range(preferred, preferred + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-daily", action="store_true", help="headless 跑一次")
    ap.add_argument("--slot", default="morning", choices=["morning", "evening", "weekly"],
                    help="早报(morning) 必跑；晚报(evening) 需 token 有余量；周报(weekly)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    db.init_db()

    if args.run_daily and args.slot == "weekly":
        # 周报：周一 09:00 生成「上一周」的按公司摘要；已经生成过就直接返回
        import weekly
        import locks
        _handle = locks.acquire("data", blocking=False)
        if _handle is None:
            print(json.dumps({"skipped": "weekly",
                              "reason": "另一个采集任务正在运行"}, ensure_ascii=False))
            return
        llm = pipeline.LLM(pipeline.apply_active_profile(pipeline.load_config()).get("llm") or {})
        t0 = time.time()
        try:
            rep = weekly.ensure_report(llm=llm)
            try:
                db.add_tokens(getattr(llm, "tokens", 0), "weekly",
                              getattr(llm, "cached_tokens", 0),
                              getattr(llm, "reasoning_tokens", 0))
            except Exception:
                pass
            print(json.dumps({"week": rep["week"], "start": rep["start"], "end": rep["end"],
                              "stories": rep["totals"]["stories"],
                              "companies": len(rep["groups"]),
                              "mode": rep["mode"], "tokens": getattr(llm, "tokens", 0)},
                             ensure_ascii=False, indent=2))
            print(f"[ok] 周报用时 {time.time()-t0:.1f}s", file=sys.stderr)
        except Exception as e:
            print(f"[fail] 周报生成失败：{e}", file=sys.stderr)
            sys.exit(1)
        finally:
            locks.release(_handle)
        return

    if args.run_daily:
        t0 = time.time()
        # 时段守卫：避免睡眠唤醒后半夜补跑 08:00 的早报（那会出现在错误的时间）
        hour = time.localtime().tm_hour
        if args.slot == "morning" and hour >= 12:
            print(json.dumps({"skipped": "morning", "reason": "已过上午时段，不补跑早报",
                              "hour": hour}, ensure_ascii=False, indent=2))
            return
        if args.slot == "evening" and hour < 16:
            print(json.dumps({"skipped": "evening", "reason": "未到傍晚时段",
                              "hour": hour}, ensure_ascii=False, indent=2))
            return
        # 晚报不是无条件跑：今天 token 已用超过阈值就跳过，并留下可查记录
        if args.slot == "evening":
            try:
                sch = (pipeline.load_config().get("schedule") or {})
            except Exception:
                sch = {}
            budget = int(sch.get("token_budget_daily") or 0)
            guard = float(sch.get("evening_guard") or 0.8)
            used = db.today_tokens()
            if budget > 0 and used >= budget * guard:
                out = {"skipped": "evening", "reason": "token 预算不足，跳过晚报",
                       "used_today": used, "budget": budget, "guard": guard}
                print(json.dumps(out, ensure_ascii=False, indent=2))
                print(f"[skip] 今日已用 {used}/{budget} token（阈值 {int(guard*100)}%），晚报跳过",
                      file=sys.stderr)
                return
        try:
            res = pipeline.run_daily(slot=args.slot)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            print(f"[ok] 用时 {time.time()-t0:.1f}s", file=sys.stderr)
        except Exception as e:
            print(f"[fail] {e}", file=sys.stderr)
            sys.exit(1)
        return

    import server
    # 小红书本机通道是内置的：配置为 local 就在后台拉起，不阻塞界面；退出时收掉
    _xhs = None
    try:
        import social
        if social.get_mode() == "local":
            import xhs_sidecar
            _xhs = xhs_sidecar
            _xhs.ensure_async()
    except Exception:
        _xhs = None
    # 端口固定 + 单实例：重开 App 回到同一个地址，旧标签页不会指向死端口
    if not args.port:
        last = saved_port() or DEFAULT_PORT
        # ① 已经有我们的实例在跑 → 直接用那个地址，不再起第二个
        for cand in ((last, DEFAULT_PORT) if last != DEFAULT_PORT else (DEFAULT_PORT,)):
            if probe_instance(cand):
                url = f"http://127.0.0.1:{cand}/"
                print(f"已在运行：{url}")
                if not args.no_open:
                    webbrowser.open(url)
                return
        # ② 默认端口优先：上一轮即便漂过，这一次也要回到用户已经打开的那个地址
        picked = 0
        for cand in ((DEFAULT_PORT, last) if last != DEFAULT_PORT else (DEFAULT_PORT,)):
            if wait_port(cand, 20):
                picked = cand
                break
        # ③ 两个都不行（被别的程序长期占着）才换新端口
        port = picked or find_free_port(max(DEFAULT_PORT, last), 20)
        save_port(port)
        args.port = port
    _explicit_port = bool(args.port)
    port = args.port or find_free_port()
    for attempt in range(3):
        try:
            srv = server.serve(port)
            break
        except OSError:
            if attempt == 2:
                raise
            wait_port(port, 6)
            port = port if can_bind(port) else find_free_port(port + 1, 20)
            args.port = port
            if not _explicit_port:
                save_port(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"LLMIntelAgent 已启动: {url}")
    print(f"数据目录: {db.app_dir()}")

    # 数据新鲜度兜底：早报/晚报因关机/睡眠没跑成时，启动后自动补一次（≥20h）
    def _catchup_later():
        time.sleep(5)
        try:
            r = server.maybe_catchup()
            if r.get("ok"):
                print(f"[catchup] 已触发启动补采（{r.get('reason')}）", file=sys.stderr)
        except Exception as e:
            print(f"[catchup] 失败：{e}", file=sys.stderr)
    if not args.run_daily:
        threading.Thread(target=_catchup_later, daemon=True).start()

    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if _xhs is not None:
            _xhs.stop()


if __name__ == "__main__":
    main()
