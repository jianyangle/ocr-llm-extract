from __future__ import annotations


class ExtractServiceError(Exception):
    """Typed extraction exception with spec-aligned error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
