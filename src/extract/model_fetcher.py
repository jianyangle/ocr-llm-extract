from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from src.extract.network_diagnostics import (
    is_timeout_exception,
    timeout_diagnostic_hint,
    timeout_seconds_text,
)
from src.extract.ollama_url import normalize_ollama_base_url
from src.extract.provider_catalog import ProviderCatalogEntry

MODEL_FETCH_TIMEOUT_SEC = 12


@dataclass(frozen=True)
class ModelFetchResult:
    ok: bool
    models: tuple[str, ...]
    error: str


def _default_get(url: str, headers: dict[str, str] | None = None, timeout: int | None = None) -> Any:
    import requests

    return requests.get(url, headers=headers, timeout=timeout)


def build_openai_models_url(base_url: str) -> str:
    base = (base_url or "").strip()
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = f"{path}/models" if path else "/models"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fetch_models(
    *,
    entry: ProviderCatalogEntry,
    base_url: str,
    api_key: str,
    http_get: Callable[..., Any] | None = None,
    timeout_sec: int = MODEL_FETCH_TIMEOUT_SEC,
) -> ModelFetchResult:
    if entry.requires_api_key and not api_key.strip():
        return ModelFetchResult(ok=False, models=(), error="请先填写 API Key 后再获取模型列表。")

    get = http_get or _default_get
    try:
        if entry.model_fetch_strategy == "ollama_tags":
            return _fetch_ollama_tags(base_url=base_url, http_get=get, timeout_sec=timeout_sec)
        return _fetch_openai_models(base_url=base_url, api_key=api_key, http_get=get, timeout_sec=timeout_sec)
    except Exception as exc:
        if is_timeout_exception(exc):
            return ModelFetchResult(
                ok=False,
                models=(),
                error=_timeout_error(
                    provider=entry.runtime_provider,
                    base_url=base_url,
                    timeout_sec=timeout_sec,
                ),
            )
        return ModelFetchResult(ok=False, models=(), error="无法连接到该平台，请检查 API 地址或网络。")


def _fetch_openai_models(
    *,
    base_url: str,
    api_key: str,
    http_get: Callable[..., Any],
    timeout_sec: int,
) -> ModelFetchResult:
    url = build_openai_models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    response = http_get(url, headers=headers, timeout=timeout_sec)
    if getattr(response, "status_code", None) != 200:
        return ModelFetchResult(ok=False, models=(), error="无法连接到该平台，请检查 API 地址或网络。")
    try:
        payload = response.json()
    except ValueError:
        return ModelFetchResult(ok=False, models=(), error="已连接，但返回的模型列表格式不受支持，可手动输入模型名。")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ModelFetchResult(ok=False, models=(), error="已连接，但返回的模型列表格式不受支持，可手动输入模型名。")
    models = _unique_nonempty_strings(
        str(item.get("id", "")).strip()
        for item in payload["data"]
        if isinstance(item, dict)
    )
    if not models:
        return ModelFetchResult(ok=False, models=(), error="已连接，但返回的模型列表格式不受支持，可手动输入模型名。")
    return ModelFetchResult(ok=True, models=models, error="")


def _fetch_ollama_tags(
    *,
    base_url: str,
    http_get: Callable[..., Any],
    timeout_sec: int,
) -> ModelFetchResult:
    root = normalize_ollama_base_url(base_url)
    response = http_get(f"{root}/api/tags", headers={}, timeout=timeout_sec)
    if getattr(response, "status_code", None) != 200:
        return ModelFetchResult(ok=False, models=(), error="无法连接到该平台，请检查 API 地址或网络。")
    try:
        payload = response.json()
    except ValueError:
        return ModelFetchResult(ok=False, models=(), error="已连接，但返回的模型列表格式不受支持，可手动输入模型名。")
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return ModelFetchResult(ok=False, models=(), error="已连接，但返回的模型列表格式不受支持，可手动输入模型名。")
    models = _unique_nonempty_strings(
        str((item.get("name") or item.get("model") or "")).strip()
        for item in payload["models"]
        if isinstance(item, dict)
    )
    if not models:
        return ModelFetchResult(ok=False, models=(), error="本地 Ollama 暂无可用模型，请先在 Ollama 中拉取模型。")
    return ModelFetchResult(ok=True, models=models, error="")


def _unique_nonempty_strings(values: object) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return tuple(output)


def _timeout_error(*, provider: str, base_url: str, timeout_sec: int) -> str:
    seconds = timeout_seconds_text(timeout_sec)
    hint = timeout_diagnostic_hint(provider=provider, base_url=base_url)
    return f"获取模型列表超时（{seconds}）。{hint}也可以手动输入模型名。"
