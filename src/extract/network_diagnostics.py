from __future__ import annotations

import platform
from urllib.parse import urlsplit


def is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    try:
        import requests
    except Exception:
        return False
    return isinstance(exc, requests.Timeout)


def timeout_seconds_text(timeout_sec: int | tuple[int, int]) -> str:
    if isinstance(timeout_sec, tuple):
        return f"{max(timeout_sec)} 秒"
    return f"{timeout_sec} 秒"


def is_localhost_url(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1"}


def runtime_environment(system: str | None = None, release: str | None = None) -> str:
    actual_system = system if system is not None else platform.system()
    actual_release = release if release is not None else platform.release()
    if actual_system == "Linux" and _is_wsl_release(actual_release):
        return "wsl"
    if actual_system == "Windows":
        return "windows"
    if actual_system == "Linux":
        return "linux"
    return "other"


def timeout_diagnostic_hint(
    *,
    provider: str,
    base_url: str,
    system: str | None = None,
    release: str | None = None,
) -> str:
    if provider == "ollama" and is_localhost_url(base_url):
        environment = runtime_environment(system=system, release=release)
        if environment == "wsl":
            return (
                "WSL 中 localhost 指向 WSL 环境；如果 Ollama 跑在 Windows，"
                "请填写 Windows 主机 IP，或确认 Ollama 监听 0.0.0.0。"
            )
        if environment == "windows":
            return "请确认 Ollama 已启动，并监听 localhost:11434。"
        if environment == "linux":
            return "请确认本机 Ollama 服务已启动。"
        return "请确认 Ollama 服务已启动。"
    return "请检查 API 地址、代理、网络或服务商状态。"


def _is_wsl_release(release: str) -> bool:
    release_text = release.lower()
    return "microsoft" in release_text or "wsl" in release_text
