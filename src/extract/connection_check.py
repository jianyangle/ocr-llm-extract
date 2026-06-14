from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.extract.model_fetcher import build_openai_models_url
from src.extract.network_diagnostics import (
    is_timeout_exception,
    timeout_diagnostic_hint,
    timeout_seconds_text,
)
from src.extract.ollama_url import normalize_ollama_base_url

CONNECTION_CHECK_TIMEOUT_SEC = 7


@dataclass(frozen=True)
class ConnectionCheckResult:
    ok: bool
    detail: str
    model_warning: str = ""


def _default_request(
    url: str, headers: dict[str, str] | None = None, timeout: int | tuple[int, int] | None = None
) -> Any:
    import requests

    return requests.get(url, headers=headers, timeout=timeout)


def check_connection(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str = "",
    http_request: Callable[..., Any] | None = None,
    timeout_sec: int | tuple[int, int] = CONNECTION_CHECK_TIMEOUT_SEC,
) -> ConnectionCheckResult:
    base = (base_url or "").strip()
    if not base:
        return ConnectionCheckResult(ok=False, detail="Base URL 未填写")

    request = http_request or _default_request

    if provider == "openai_compatible":
        url = build_openai_models_url(base)
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "ollama":
        root = normalize_ollama_base_url(base)
        url = f"{root}/api/tags"
        headers = {}
    else:
        return ConnectionCheckResult(ok=False, detail=f"未知 provider: {provider}")

    try:
        response = request(url, headers=headers, timeout=timeout_sec)
    except Exception as exc:
        if is_timeout_exception(exc):
            return ConnectionCheckResult(ok=False, detail=_timeout_detail(provider, base, timeout_sec))
        return ConnectionCheckResult(ok=False, detail=f"无法连接到 {base}")

    code = getattr(response, "status_code", None)

    if code == 200:
        warning = _check_model_in_response(response, model, provider)
        return ConnectionCheckResult(ok=True, detail="连接正常", model_warning=warning)
    if code in (401, 403):
        return ConnectionCheckResult(ok=False, detail="API Key 无效或无权限")
    return ConnectionCheckResult(ok=False, detail=f"服务返回 {code}")


def _check_model_in_response(response: Any, model: str, provider: str) -> str:
    model = (model or "").strip()
    if not model:
        return ""
    try:
        body = response.json()
    except Exception:
        return ""
    models = _extract_model_ids(body, provider)
    if not models:
        return ""
    if _model_matches(model, models, provider):
        return ""
    return f"模型「{model}」不在可用列表中，请确认名称是否正确。"


def _extract_model_ids(body: Any, provider: str) -> list[str]:
    if provider == "ollama":
        items = _model_items(body, "models")
        return [
            model_id
            for item in items
            if isinstance(item, dict)
            for model_id in (_model_id_text(item.get("name")), _model_id_text(item.get("model")))
            if model_id
        ]
    items = _model_items(body, "data")
    return [
        model_id
        for item in items
        if isinstance(item, dict)
        for model_id in [_model_id_text(item.get("id"))]
        if model_id
    ]


def _model_items(body: Any, key: str) -> list[Any]:
    if not isinstance(body, dict):
        return []
    items = body.get(key, [])
    if not isinstance(items, list):
        return []
    return items


def _model_id_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _model_matches(model: str, available: list[str], provider: str) -> bool:
    if model in available:
        return True
    if provider == "ollama":
        return any(item.split(":")[0] == model for item in available)
    return False


def _timeout_detail(provider: str, base_url: str, timeout_sec: int | tuple[int, int]) -> str:
    seconds = timeout_seconds_text(timeout_sec)
    hint = timeout_diagnostic_hint(provider=provider, base_url=base_url)
    return f"连接超时（{seconds}）。{hint}"
