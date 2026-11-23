"""跨进程数据锁（flock）。

采集会同时被 launchd（早报/晚报/周报）和 GUI 触发。SQLite 的 WAL 能保证单条写入安全，
但两个完整采集同时跑会重复抓取、重复计 token，并在去重阶段互相踩踏。
所以整个采集阶段用一把文件锁串行化：

  - 拿不到锁：立即返回（非阻塞），调用方把本次标记为 skipped=locked；
  - 进程崩溃：内核自动释放 flock，不会留下死锁。
"""
import fcntl
import os
import time

import db


def _path(name: str) -> str:
    d = db.app_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.lock")


def acquire(name: str = "data", blocking: bool = False, timeout: float = 0):
    """返回打开的文件句柄（持有锁）或 None。调用 release() 释放。"""
    f = open(_path(name), "a+")
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                f.seek(0)
                f.truncate()
                f.write(f"pid={os.getpid()} at={db.now_iso()}\n")
                f.flush()
            except Exception:
                pass
            return f
        except BlockingIOError:
            if not blocking or time.time() >= deadline:
                f.close()
                return None
            time.sleep(0.3)
        except Exception:
            f.close()
            return None


def release(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def held(name: str = "data") -> bool:
    """只探测，不持锁（给 /api/status 用）。"""
    try:
        f = open(_path(name), "a+")
    except Exception:
        return False
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except Exception:
        return False
    finally:
        f.close()
