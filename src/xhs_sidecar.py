"""内置 xiaohongshu-mcp 进程管理。

随 app 分发（Apache-2.0，见 vendor/XIAOHONGSHU-MCP-LICENSE），用时拉起、退出时收掉：
  - 端口固定 18060；已有健康实例（用户自己启的）就复用，不重复拉起
  - 首次启动会下载内置浏览器（~180MB，缓存到 ~/Library/Caches/xiaohongshu-mcp），日志可查
  - 只在 channels.xiaohongshu_mode == "local" 时启动；默认 off 不碰
"""
import atexit
import os
import subprocess
import sys
import threading
import time
import urllib.request

import db

PORT = 18060
BASE = os.environ.get("XHS_MCP_BASE", f"http://127.0.0.1:{PORT}").rstrip("/")

_LOCK = threading.Lock()
_PROC = None
_SPAWNED_AT = 0.0
_LAST_ERROR = ""
_AUTO_DONE = False


def _log_path() -> str:
    d = os.path.join(db.app_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "xhs-sidecar.log")


def log_tail(n: int = 700) -> str:
    try:
        with open(_log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n * 3))
            data = f.read()
        return data.decode("utf-8", "ignore")[-n:]
    except Exception:
        return ""


def binary_path() -> str:
    if getattr(sys, "frozen", False):
        cands = [os.path.join(sys._MEIPASS, "bin", "xiaohongshu-mcp"),
                 os.path.join(os.path.dirname(sys.executable), "..", "Frameworks",
                              "bin", "xiaohongshu-mcp")]
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cands = [os.path.join(root, "build", "vendor", "xiaohongshu-mcp")]
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return ""


def healthy(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _port_busy() -> bool:
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def _runtime_dir() -> str:
    """sidecar 的可写工作目录（cookies.json 等落这里）。

    绝不能放在 app 包内：往 Contents/Frameworks/bin 写文件会破坏代码签名封印，
    下次启动 Gatekeeper 就会报「sealed resource is missing or invalid」。
    """
    d = os.path.join(db.app_dir(), "xhs-runtime")
    os.makedirs(d, exist_ok=True)
    return d


def start() -> dict:
    """幂等启动。返回 {running / starting / error}，不等待健康（下载浏览器可能几分钟）。"""
    global _PROC, _SPAWNED_AT, _LAST_ERROR
    with _LOCK:
        if healthy():
            return {"running": True, "starting": False}
        if _PROC is not None and _PROC.poll() is None:
            return {"running": False, "starting": True}
        if _port_busy():
            _LAST_ERROR = f"端口 {PORT} 被占用，但不是可用的 xiaohongshu-mcp"
            return {"running": False, "starting": False, "error": _LAST_ERROR}
        binp = binary_path()
        if not binp:
            _LAST_ERROR = "内置服务缺失（打包时未带 bin/xiaohongshu-mcp）"
            return {"running": False, "starting": False, "error": _LAST_ERROR}
        try:
            lf = open(_log_path(), "ab", buffering=0)
            lf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {binp}\n".encode())
            _PROC = subprocess.Popen(
                [binp, "-headless", "-port", f":{PORT}"],
                stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                cwd=_runtime_dir(), start_new_session=True)
            _SPAWNED_AT = time.time()
            _LAST_ERROR = ""
        except Exception as e:
            _LAST_ERROR = f"启动失败：{str(e)[:160]}"
            return {"running": False, "starting": False, "error": _LAST_ERROR}
    return {"running": False, "starting": True}


def state() -> dict:
    """给 UI 的状态：运行中 / 启动中 / 失败（带日志尾）。"""
    running = healthy()
    starting = False
    err = _LAST_ERROR
    if not running and _PROC is not None:
        rc = _PROC.poll()
        if rc is not None:
            err = err or f"进程已退出（code={rc}）"
        else:
            starting = True
    return {"running": running, "starting": starting, "error": err,
            "log": log_tail() if not running else "",
            "binary": binary_path(), "base": BASE}


def ensure_async() -> None:
    """应用启动时调用：local 模式就后台拉起，不阻塞 UI。"""
    global _AUTO_DONE
    if _AUTO_DONE:
        return
    _AUTO_DONE = True
    if not healthy():
        threading.Thread(target=start, daemon=True).start()


def restart() -> dict:
    stop()
    global _PROC
    _PROC = None
    return start()


def stop() -> None:
    global _PROC
    p = _PROC
    if p is None:
        return
    try:
        if p.poll() is None:
            p.terminate()
            for _ in range(30):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if p.poll() is None:
                p.kill()
    except Exception:
        pass
    _PROC = None


atexit.register(stop)
