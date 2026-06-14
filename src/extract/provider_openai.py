from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .errors import ExtractServiceError

OPENAI_MAX_WORKERS = 4
OPENAI_CONNECTION_RETRIES = 1


class OpenAICompatibleAdapter:
    def __init__(
        self,
        http_post: Callable[..., Any] | None = None,
        timeout_sec: int = 90,
        event_logger: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._http_post = http_post
        self._timeout_sec = timeout_sec
        self._event_logger = event_logger
        self._connection_retries = OPENAI_CONNECTION_RETRIES

    def chat(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        prompt_cache: bool = False,
        thinking_disable_params: dict[str, Any] | None = None,
    ) -> str:
        post = self._http_post or _requests_post
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        mode = "json_schema" if schema else "none"

        while True:
            payload = {
                "model": model,
                "messages": _prepare_messages(messages, prompt_cache=prompt_cache),
                "temperature": 0,
                "stream": False,
            }
            if seed is not None:
                payload["seed"] = seed
            response_format = _response_format_for_mode(mode, schema=schema)
            if response_format is not None:
                payload["response_format"] = response_format
            if thinking_disable_params is not None:
                payload.update(thinking_disable_params.get("extra_body", {}))

            response = self._post_with_retry(
                post,
                url=url,
                headers=headers,
                payload=payload,
            )

            next_mode = _next_schema_mode(mode)
            if schema and next_mode is not None and _is_schema_unsupported(response):
                self._record_downgrade(mode, next_mode)
                mode = next_mode
                continue

            self._raise_for_status(response.status_code)

            try:
                body = response.json()
            except Exception as exc:
                raise ExtractServiceError("E_LLM_002", "LLM response is not valid JSON") from exc

            content = _safe_get(body, "choices", 0, "message", "content")
            if not isinstance(content, str):
                raise ExtractServiceError("E_LLM_002", "LLM response content is missing")
            return content

    def chat_batch(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages_list: list[list[dict[str, Any]]],
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        prompt_cache: bool = False,
        thinking_disable_params: dict[str, Any] | None = None,
        max_workers: int = OPENAI_MAX_WORKERS,
    ) -> list[str]:
        if not messages_list:
            return []

        results: list[str | None] = [None] * len(messages_list)
        worker_count = min(max_workers, len(messages_list))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self.chat,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    schema=schema,
                    seed=seed,
                    prompt_cache=prompt_cache,
                    thinking_disable_params=thinking_disable_params,
                ): index
                for index, messages in enumerate(messages_list)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception:
                    for pending in futures:
                        pending.cancel()
                    raise
        return [result for result in results if result is not None]

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

    def _record_downgrade(self, current_mode: str, next_mode: str) -> None:
        if self._event_logger is None:
            return
        self._event_logger(
            "extract.schema_downgrade",
            {
                "from": current_mode,
                "to": next_mode,
            },
        )

    def _post_with_retry(
        self,
        post: Callable[..., Any],
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        last_error: Exception | None = None
        total_attempts = self._connection_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                return post(url, headers=headers, json=payload, timeout=self._timeout_sec)
            except ExtractServiceError:
                raise
            except Exception as exc:
                last_error = exc
                self._record_connection_issue(
                    attempt=attempt,
                    total_attempts=total_attempts,
                    error=_format_connection_error(exc),
                )
                if attempt >= total_attempts:
                    break

        detail = _format_connection_error(last_error)
        raise ExtractServiceError("E_LLM_001", f"LLM connection failed: {detail}") from last_error

    def _record_connection_issue(self, *, attempt: int, total_attempts: int, error: str) -> None:
        if self._event_logger is None:
            return
        self._event_logger(
            "extract.connection_issue",
            {
                "attempt": attempt,
                "total_attempts": total_attempts,
                "error": error,
            },
        )


def _safe_get(data: Any, *path: Any) -> Any:
    current = data
    for item in path:
        if isinstance(item, int):
            if not isinstance(current, list) or item >= len(current):
                return None
            current = current[item]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(item)
    return current


def _requests_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: int) -> Any:
    import requests

    return requests.post(url, headers=headers, json=json, timeout=timeout)


def _response_format_for_mode(mode: str, *, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None or mode == "none":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "extract_rows",
            "schema": schema,
            "strict": True,
        },
    }


def _prepare_messages(messages: list[dict[str, Any]], *, prompt_cache: bool) -> list[dict[str, Any]]:
    if not prompt_cache:
        return messages

    prepared: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "system" or not isinstance(message.get("content"), str):
            prepared.append(message)
            continue
        prepared.append(
            {
                **message,
                "content": [
                    {
                        "type": "text",
                        "text": message["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        )
    return prepared


def _next_schema_mode(mode: str) -> str | None:
    if mode == "json_schema":
        return "json_object"
    if mode == "json_object":
        return "none"
    return None


def _is_schema_unsupported(response: Any) -> bool:
    if getattr(response, "status_code", None) not in {400, 422}:
        return False
    text = str(getattr(response, "text", "") or "").lower()
    return any(keyword in text for keyword in ("response_format", "json_schema", "json_object", "unsupported"))


def _format_connection_error(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    message = str(exc).strip()
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"
