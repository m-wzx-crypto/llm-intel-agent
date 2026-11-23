"""一键诊断包：把排障需要的信息打成一个 zip。

不包含 API Key（配置里所有 key/secret/token/password 字段一律替换为 ***），
日志只截取尾部，避免把整年日志塞进包里。
"""
import json
import os
import platform
import sys
import time
import zipfile

import db

_SECRET_KEYS = {"api_key", "secret", "password", "token", "access_token",
                "refresh_token", "client_secret"}


def _redact(o):
    if isinstance(o, dict):
        return {k: ("***" if (str(k).lower() in _SECRET_KEYS and isinstance(v, str) and v)
                    else _redact(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(x) for x in o]
    return o


def _dump(name: str, obj, zf: zipfile.ZipFile) -> None:
    zf.writestr(name, json.dumps(obj, ensure_ascii=False, indent=2))


def _db_stats() -> dict:
    out = {}
    conn = db.connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = None
    finally:
        conn.close()
    return out


def _tail(path: str, limit: int = 300_000) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read()
    except Exception:
        return b""


def build(state: dict | None = None) -> dict:
    import pipeline
    import version
    root = os.path.join(db.app_dir(), "diagnostics")
    os.makedirs(root, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(root, f"LLMIntelAgent-diag-{stamp}.zip")

    cfg = {}
    try:
        cfg = _redact(pipeline.load_config())
    except Exception as e:
        cfg = {"error": str(e)[:200]}

    runs, probes = [], []
    conn = db.connect()
    try:
        runs = [dict(r) for r in conn.execute(
            "SELECT id,started_at,finished_at,status,new_items,sources_ok,sources_bad,"
            "cost_tokens,note FROM run ORDER BY id DESC LIMIT 50")]
        last = conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            probes = [dict(r) for r in conn.execute(
                "SELECT source_id,source_name,status,hit_count,http_status,latency_ms,error,"
                "checked_at FROM probe_run WHERE run_id=? ORDER BY id", (last["id"],))]
    except Exception:
        pass
    finally:
        conn.close()

    try:
        import social
        xhs = {"state": social.collect_state(), "probe": social.probe()}
    except Exception as e:
        xhs = {"error": str(e)[:200]}

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version.txt", f"{version.__version__}\n")
        _dump("system.json", {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "app_dir": db.app_dir(),
            "time": db.now_iso(),
        }, zf)
        _dump("config.redacted.json", cfg, zf)
        _dump("freshness.json", db.freshness(), zf)
        _dump("runs.json", runs, zf)
        _dump("probes.last_run.json", probes, zf)
        _dump("xhs.json", xhs, zf)
        _dump("db_stats.json", _db_stats(), zf)
        if state is not None:
            _dump("runtime_state.json", _redact(state), zf)
        logdir = os.path.join(db.app_dir(), "logs")
        if os.path.isdir(logdir):
            for fn in sorted(os.listdir(logdir))[:20]:
                fp = os.path.join(logdir, fn)
                if os.path.isfile(fp):
                    data = _tail(fp)
                    if data:
                        zf.writestr(f"logs/{fn}", data)
    return {"ok": True, "path": path, "size": os.path.getsize(path),
            "at": db.now_iso()}
