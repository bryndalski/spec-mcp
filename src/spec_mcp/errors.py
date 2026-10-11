"""Structured errors: every tool returns {"error": {...}} instead of raising."""

from __future__ import annotations

from typing import Any, Literal

Category = Literal["not_found", "validation", "io", "config"]


class SpecError(Exception):
    def __init__(self, category: Category, message: str, retryable: bool = False, **extra: Any):
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = retryable
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
        }
        err.update(self.extra)
        return {"error": err}
