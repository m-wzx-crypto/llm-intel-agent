#!/usr/bin/env python3
"""远程推送更新（部署方案 §8.6 三段式：检查 → 下载 → 校验 → 安装 → 回滚）

设计约束来自 docs/部署方案_v0.4.md §8.6：

1. 清单托管在国内静态源（客户在国内，GitHub 下载不通）；
2. 下载后**必须**校验 sha256 —— 不校验等于给中间人开门；
3. 覆盖前把旧 .app 改名保留、升级前备份 data.db，失败要能回滚；
4. mandatory 只在"旧版会写坏数据"时使用，否则客户反感。

本模块只提供机制，不做 UI：server.py 把下面几个函数暴露成 /api/update/*，
前端（web/index.html）负责红点与确认交互。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import db
from version import __version__

# 清单地址：环境变量 > 用户配置 update.manifest_url > 这里的默认值
DEFAULT_MANIFEST_URL = "https://cmgl.top/llmintel/latest.json"
CHECK_INTERVAL = 6 * 3600          # 静默检查间隔（启动时另有一次强制检查）
HTTP_TIMEOUT = 20
MAX_DMG_BYTES = 600 * 1024 * 1024  # 600MB 上限，防清单被改成指向巨物
UA = f"LLMIntelAgent/{__version__} (macOS)"

_LOCK = threading.Lock()

# 供前端轮询的运行时状态（进程内，不落盘）
STATE: dict = {
    "checking": False,
    "downloading": False,
    "installing": False,
    "pct": 0,
    "msg": "",
    "error": None,
}

_STATE_CACHE: dict = {"loaded": False, "last_check": 0.0, "manifest": None,
                      "skipped_version": ""}


# ---------------------------------------------------------------- 路径与配置

def update_dir() -> str:
    """下载缓存目录：~/Library/Caches/LLMIntelAgent/update/"""
    base = os.path.join(os.path.expanduser("~"), "Library", "Caches", db.APP_NAME, "update")
    os.makedirs(base, exist_ok=True)
    return base


def _state_file() -> str:
    return os.path.join(db.app_dir(), "update_state.json")


def current_version() -> str:
    return __version__


def _read_user_config() -> dict:
    """只读配置里的 update 段；刻意不走 pipeline.load_config，避免把采集链拉起来。"""
    try:
        p = db.config_path()
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def manifest_urls() -> list[str]:
    """按优先级返回可用的清单地址：环境变量 > 配置主地址 > 配置镜像。"""
    out = []
    env = (os.environ.get("LLMINTEL_UPDATE_URL") or "").strip()
    if env:
        out.append(env)
    upd = (_read_user_config().get("update") or {})
    primary = (upd.get("manifest_url") or "").strip()
    if primary:
        out.append(primary)
    for u in (upd.get("mirror_urls") or []):
        u = str(u or "").strip()
        if u:
            out.append(u)
    if not out:
        out.append(DEFAULT_MANIFEST_URL)
    return list(dict.fromkeys(out))


def manifest_url() -> str:
    return manifest_urls()[0]


# ---------------------------------------------------------------- 版本比对

def _vtuple(v: str) -> tuple:
    out = []
    for part in str(v or "0").strip().lstrip("vV").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _os_ok(min_os: str) -> bool:
    """清单里的 min_os 是 macOS 版本门槛（如 13.0）。"""
    try:
        import platform
        cur = platform.mac_ver()[0] or "0"
        return _vtuple(cur) >= _vtuple(min_os or "0")
    except Exception:
        return True


# ---------------------------------------------------------------- 落盘状态

def _load_cache() -> dict:
    if _STATE_CACHE["loaded"]:
        return _STATE_CACHE
    try:
        with open(_state_file(), encoding="utf-8") as f:
            data = json.load(f) or {}
        man = data.get("manifest")
        if man is not None:
            try:
                man = _validate_manifest(man)
            except Exception:
                man = None
        _STATE_CACHE.update(
            last_check=float(data.get("last_check") or 0),
            manifest=man,
            skipped_version=str(data.get("skipped_version") or ""),
            loaded=True,
        )
    except Exception:
        _STATE_CACHE["loaded"] = True
    return _STATE_CACHE


def _save_cache() -> None:
    try:
        with open(_state_file(), "w", encoding="utf-8") as f:
            json.dump({"last_check": _STATE_CACHE["last_check"],
                       "manifest": _STATE_CACHE["manifest"],
                       "skipped_version": _STATE_CACHE.get("skipped_version") or ""},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def info() -> dict:
    """给「版本与更新」面板：当前版本 + 上次检查时间 + 已跳过版本。"""
    cache = _load_cache()
    last = float(cache.get("last_check") or 0)
    return {
        "version": current_version(),
        "manifest_urls": manifest_urls(),
        "last_check": last,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "",
        "skipped_version": cache.get("skipped_version") or "",
    }


def skip(version: str) -> dict:
    """用户选择「跳过此版本」：下次不再提示，直到出现更高版本。"""
    ver = str(version or "").strip()
    if not _vtuple(ver) > (0, 0, 0):
        return {"ok": False, "error": "版本号无效"}
    with _LOCK:
        cache = _load_cache()
        cache["skipped_version"] = ver
        _save_cache()
    return {"ok": True, "skipped_version": ver}


# ---------------------------------------------------------------- ① 检查

def _validate_manifest(man) -> dict:
    """清单是外部输入：字段、版本、地址、哈希都校验，不信任远端。"""
    if not isinstance(man, dict):
        raise ValueError("清单不是 JSON 对象")
    ver = str(man.get("version") or "").strip()
    if not _vtuple(ver) > (0, 0, 0):
        raise ValueError("version 不是 x.y.z")
    url = str(man.get("dmg_url") or "").strip()
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("dmg_url 必须是 http(s) 绝对地址")
    if p.scheme == "http" and p.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("非本机地址必须用 https")
    sha = str(man.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("sha256 不是 64 位十六进制")
    try:
        size = int(man.get("size") or 0)
    except Exception:
        raise ValueError("size 不是整数")
    if size <= 0 or size > 2 * 1024 ** 3:
        raise ValueError("size 超出合理范围")
    out = dict(man)
    out.update(version=ver, dmg_url=url, sha256=sha, size=size)
    return out


def _fetch_manifest(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read(256 * 1024)
    return _validate_manifest(json.loads(raw.decode("utf-8")))


def check(force: bool = False) -> dict:
    """拉清单并比对。返回给前端的统一结构；任何异常都降级为 error 字段，不抛。

    依次尝试主地址与镜像；全部失败时若本地有旧清单，仍返回旧判定并标 stale，
    避免离线/网络抖动时右上角红点无故消失。
    """
    cache = _load_cache()
    now = time.time()

    if not force and cache["manifest"] and (now - cache["last_check"]) < CHECK_INTERVAL:
        return _decide(cache["manifest"], cached=True)

    with _LOCK:
        if STATE["checking"]:
            return _decide(cache["manifest"], cached=True)
        STATE["checking"] = True
    errors = []
    try:
        for url in manifest_urls():
            try:
                man = _fetch_manifest(url)
            except Exception as e:
                errors.append(f"{url} → {type(e).__name__}: {e}")
                continue
            cache["manifest"] = man
            cache["last_check"] = now
            _save_cache()
            res = _decide(man)
            res["source"] = url
            return res
        err = "；".join(errors)[-400:]
        if cache["manifest"]:
            res = _decide(cache["manifest"], cached=True)
            res.update(ok=False, stale=True, error=err)
            return res
        return {
            "ok": False,
            "error": err or "没有可用的清单地址",
            "current": current_version(),
            "manifest_urls": manifest_urls(),
            "available": False,
        }
    finally:
        with _LOCK:
            STATE["checking"] = False


def _decide(man: dict | None, cached: bool = False) -> dict:
    cur = current_version()
    if not man:
        return {"ok": True, "available": False, "current": cur,
                "manifest_urls": manifest_urls(), "cached": cached}
    remote = str(man.get("version") or "0")
    cache = _load_cache()
    skipped = str(cache.get("skipped_version") or "")
    available = _vtuple(remote) > _vtuple(cur)
    is_skipped = bool(skipped) and _vtuple(remote) == _vtuple(skipped) \
        and not bool(man.get("mandatory"))
    if is_skipped:
        available = False
    last = float(cache.get("last_check") or 0)
    return {
        "ok": True,
        "available": available,
        "skipped": is_skipped,
        "skipped_version": skipped,
        "cached": cached,
        "current": cur,
        "version": remote,
        "notes": man.get("notes") or "",
        "mandatory": bool(man.get("mandatory")),
        "size": int(man.get("size") or 0),
        "published_at": man.get("published_at") or "",
        "min_os": man.get("min_os") or "",
        "min_os_ok": _os_ok(man.get("min_os")),
        "manifest_url": manifest_url(),
        "manifest_urls": manifest_urls(),
        "last_check": last,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "",
    }


# ---------------------------------------------------------------- ② 下载 + 校验

def download() -> dict:
    """下载 dmg 到缓存目录并校验 sha256。前端轮询 STATE 看进度。"""
    man = _load_cache()["manifest"]
    if not man:
        return {"ok": False, "error": "没有清单，请先检查更新"}

    ver = str(man.get("version") or "0")
    url = man["dmg_url"]
    want_sha = str(man.get("sha256") or "").lower()
    want_size = int(man.get("size") or 0)

    target = os.path.join(update_dir(), f"LLMIntelAgent-{ver}.dmg")
    if os.path.exists(target) and _sha256(target) == want_sha:
        with _LOCK:
            STATE.update(downloading=False, pct=100, msg="已下载", error=None,
                         dmg_path=target, ready=True)
        return {"ok": True, "path": target, "msg": "已有校验通过的安装包"}

    with _LOCK:
        if STATE["downloading"]:
            return {"ok": False, "error": "正在下载中"}
        STATE.update(downloading=True, pct=0, msg="连接下载源", error=None, ready=False)

    tmp = target + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        h = hashlib.sha256()
        done = 0
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or want_size or 0)
            if total > MAX_DMG_BYTES:
                raise ValueError(f"安装包异常过大：{total} 字节")
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > MAX_DMG_BYTES:
                    raise ValueError("下载超过体积上限，已中止")
                pct = int(done * 100 / total) if total else 0
                with _LOCK:
                    STATE.update(pct=min(pct, 99), msg=f"下载中 {done // 1048576}MB")

        got = h.hexdigest()
        if want_sha and got != want_sha:
            raise ValueError(f"sha256 不匹配（期望 {want_sha[:12]}…，实际 {got[:12]}…）")
        if want_size and done != want_size:
            raise ValueError(f"大小不符（期望 {want_size}，实际 {done}）")

        os.replace(tmp, target)
        with _LOCK:
            STATE.update(downloading=False, pct=100, msg="下载并校验完成",
                         dmg_path=target, ready=True, error=None)
        return {"ok": True, "path": target, "size": done}
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        with _LOCK:
            STATE.update(downloading=False, pct=0, msg="", error=f"{type(e).__name__}: {e}",
                         ready=False)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- ③ 安装 + 回滚

def app_bundle() -> str | None:
    """当前运行的 .app 路径；开发模式（裸 python 跑源码）返回 None。"""
    p = os.path.realpath(sys.executable)
    for _ in range(6):
        p = os.path.dirname(p)
        if not p or p == "/":
            break
        if p.endswith(".app"):
            return p
    return None


_INSTALL_SCRIPT = r"""#!/bin/bash
# 由 LLMIntelAgent 更新器生成：等旧进程退出 → 备份 db → 换 .app → 重启，失败即回滚
set -u
APP="$1"; DMG="$2"; BAK="$3"; PID="$4"; MNT="$5"; LOG="$6"
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

log "等待旧进程 $PID 退出"
for i in $(seq 1 240); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$PID" 2>/dev/null; then
  log "旧进程 120s 未退出，放弃更新"
  open -a "$APP"
  exit 1
fi

if ! hdiutil attach "$DMG" -nobrowse -quiet -mountpoint "$MNT" >>"$LOG" 2>&1; then
  log "挂载 dmg 失败"
  open -a "$APP"; exit 1
fi

SRC="$(/bin/ls -d "$MNT"/*.app 2>/dev/null | head -1)"
if [ -z "$SRC" ]; then
  log "dmg 内未找到 .app"
  hdiutil detach "$MNT" -quiet >>"$LOG" 2>&1
  open -a "$APP"; exit 1
fi

log "保留旧版本 -> $BAK"
rm -rf "$BAK"
if ! mv "$APP" "$BAK" >>"$LOG" 2>&1; then
  log "旧版本改名失败，放弃（此时系统里还没有被破坏的东西）"
  hdiutil detach "$MNT" -quiet >>"$LOG" 2>&1
  open -a "$APP"; exit 1
fi

if ! ditto "$SRC" "$APP" >>"$LOG" 2>&1; then
  log "复制新版本失败，回滚"
  hdiutil detach "$MNT" -quiet >>"$LOG" 2>&1
  rm -rf "$APP"
  mv "$BAK" "$APP" >>"$LOG" 2>&1
  open -a "$APP"; exit 1
fi

hdiutil detach "$MNT" -quiet >>"$LOG" 2>&1
# dmg 是公证+staple 过的，这里只是去掉下载带来的 quarantine 标记
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
log "更新完成，重新拉起"
open -a "$APP"
"""


def _backup_db() -> str | None:
    """升级前备份 data.db（更新丢数据是唯一不可挽回的事故）。"""
    try:
        src = db.db_path()
        if not os.path.exists(src):
            return None
        bdir = os.path.join(db.app_dir(), "backups")
        os.makedirs(bdir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(bdir, f"data-{current_version()}-{ts}.db")
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


def install() -> dict:
    """把已下载好的 dmg 装上。返回后由 server 负责让当前进程退出。"""
    man = _load_cache()["manifest"] or {}
    ver = str(man.get("version") or "0")
    dmg = os.path.join(update_dir(), f"LLMIntelAgent-{ver}.dmg")
    if not os.path.exists(dmg):
        return {"ok": False, "error": "安装包不在缓存里，请先下载"}
    want_sha = str(man.get("sha256") or "").lower()
    if want_sha and _sha256(dmg) != want_sha:
        return {"ok": False, "error": "安装包校验失败，请重新下载"}

    app = app_bundle()
    if not app:
        return {"ok": False, "error": "开发模式下不执行自动安装；请手动打开 dmg 安装"}

    db_backup = _backup_db()

    mnt = tempfile.mkdtemp(prefix="llmintel-upd-")
    log = os.path.join(update_dir(), "install.log")
    bak = f"{app}.bak-{current_version()}"
    script = os.path.join(update_dir(), "install.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_INSTALL_SCRIPT)
    os.chmod(script, 0o755)

    with _LOCK:
        STATE.update(installing=True, msg="准备重启安装", error=None)

    args = [script, app, dmg, bak, str(os.getpid()), mnt, log]
    try:
        subprocess.Popen(["/bin/bash", *args],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         close_fds=True)
    except Exception as e:
        with _LOCK:
            STATE.update(installing=False, error=f"启动安装脚本失败：{e}")
        return {"ok": False, "error": f"启动安装脚本失败：{e}"}

    return {"ok": True, "msg": "安装脚本已启动，应用即将退出并自动重开",
            "db_backup": db_backup, "log": log, "old_app_backup": bak}


def progress() -> dict:
    with _LOCK:
        return dict(STATE)
