from __future__ import annotations

from typing import Any, Callable

from .errors import ExtractServiceError
from .ollama_url import normalize_ollama_base_url

OLLAMA_MAX_WORKERS = 1


class OllamaAdapter:
    def __init__(self, http_post: Callable[..., Any] | None = None, timeout_sec: int = 90) -> None:
        self._http_post = http_post
        self._timeout_sec = timeout_sec

    def chat(
        self,
        *,
        base_url: str,
        model: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        prompt_cache: bool = False,
        num_ctx: int | None = None,
        thinking_disable_params: dict[str, Any] | None = None,
    ) -> str:
        _ = prompt_cache
        post = self._http_post or _requests_post
        url = f"{normalize_ollama_base_url(base_url)}/api/chat"
        headers = {
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0},
        }
        if seed is not None:
            payload["options"]["seed"] = seed
        if num_ctx is not None:
            payload["options"]["num_ctx"] = int(num_ctx)
        if schema is not None:
            payload["format"] = schema
        if thinking_disable_params:
            option_params = thinking_disable_params.get("options", {})
            if isinstance(option_params, dict):
                payload["options"].update(option_params)
            payload.update(
                {
                    key: value
                    for key, value in thinking_disable_params.items()
                    if key != "options"
                }
            )

        try:
            response = post(url, headers=headers, json=payload, timeout=self._timeout_sec)
        except ExtractServiceError:
            raise
        except Exception as exc:
            raise ExtractServiceError("E_LLM_001", "LLM connection failed") from exc

        self._raise_for_status(response.status_code)

        try:
            body = response.json()
        except Exception as exc:
            raise ExtractServiceError("E_LLM_002", "LLM response is not valid JSON") from exc

        message = body.get("message") if isinstance(body, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ExtractServiceError("E_LLM_002", "LLM response content is missing")
        return content

    def chat_batch(
        self,
        *,
        base_url: str,
        model: str,
        messages_list: list[list[dict[str, Any]]],
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        prompt_cache: bool = False,
        num_ctx: int | None = None,
        max_workers: int = OLLAMA_MAX_WORKERS,
        thinking_disable_params: dict[str, Any] | None = None,
    ) -> list[str]:
        _ = max_workers
        results: list[str] = []
        for messages in messages_list:
            results.append(
                self.chat(
                    base_url=base_url,
                    model=model,
                    messages=messages,
                    schema=schema,
                    seed=seed,
                    prompt_cache=prompt_cache,
                    num_ctx=num_ctx,
                    thinking_disable_params=thinking_disable_params,
                )
            )
        return results

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code == 200:
            return
        if status_code in (400, 422):
            raise ExtractServiceError("E_LLM_003", "LLM request parameters invalid")
        if status_code in (401, 403):
            raise ExtractServiceError("E_LLM_004", "LLM authentication failed")
        if status_code == 429:
            raise ExtractServiceError("E_LLM_005", "LLM rate limited")
        if status_code >= 500:
            raise ExtractServiceError("E_LLM_006", "LLM provider server error")
        raise ExtractServiceError("E_LLM_006", f"Unexpected LLM status code: {status_code}")


def _requests_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: int) -> Any:
    import requests

    return requests.post(url, headers=headers, json=json, timeout=timeout)
