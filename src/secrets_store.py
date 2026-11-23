"""API Key 的本机安全存储。

目标：配置文件里不再留明文 Key。macOS 用系统 Keychain（`security` CLI），
其他平台或 Keychain 不可用时自动退回 config（行为不变，不阻断启动）。

约定：
  - service 固定 LLMIntelAgent
  - account 用 "active" 存当前生效 Key；用 "profile:<id>" 存各档案 Key
  - 环境变量 LLMINTEL_KEYCHAIN=0 可强制关闭（测试 / 干净机器排障）
"""
import os
import shutil
import subprocess
import sys

SERVICE = "LLMIntelAgent"


def enabled_by_env() -> bool:
    return os.environ.get("LLMINTEL_KEYCHAIN", "1").strip().lower() not in (
        "0", "false", "off", "no")


def available() -> bool:
    return (sys.platform == "darwin" and enabled_by_env()
            and shutil.which("security") is not None)


def _run(args: list[str], timeout: float = 6) -> subprocess.CompletedProcess:
    return subprocess.run(["security"] + args, capture_output=True, text=True,
                          timeout=timeout)


def get_key(account: str) -> str:
    if not account or not available():
        return ""
    try:
        p = _run(["find-generic-password", "-a", account, "-s", SERVICE, "-w"])
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def set_key(account: str, key: str) -> bool:
    """写入/更新 Keychain；成功返回 True。空 Key 视为删除。"""
    if not account or not available():
        return False
    if not key:
        return delete_key(account)
    try:
        p = _run(["add-generic-password", "-U", "-a", account, "-s", SERVICE,
                  "-w", key])
        return p.returncode == 0
    except Exception:
        return False


def delete_key(account: str) -> bool:
    if not account or not available():
        return False
    try:
        p = _run(["delete-generic-password", "-a", account, "-s", SERVICE])
        # 不存在也算删除成功（幂等）
        return p.returncode == 0 or "could not be found" in (p.stderr or "")
    except Exception:
        return False


def status() -> dict:
    return {"available": available(), "service": SERVICE,
            "enabled": enabled_by_env(), "platform": sys.platform}
