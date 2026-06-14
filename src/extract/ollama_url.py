from __future__ import annotations


OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"


def normalize_ollama_base_url(value: str) -> str:
    root = (value or "").strip().rstrip("/")
    suffix = "/api/chat"
    if root.endswith(suffix):
        root = root[: -len(suffix)].rstrip("/")
    return root
