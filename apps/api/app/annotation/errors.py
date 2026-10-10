"""Typed API errors: {"error": {"code", "message", "current_annotation_version", "details"}}."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, details: list[dict[str, Any]] | None = None,
                 current_annotation_version: int | str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        self.current_annotation_version = current_annotation_version
        self.extra = extra

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message, "details": self.details}
        if self.current_annotation_version is not None:
            error["current_annotation_version"] = str(self.current_annotation_version)
        error.update(self.extra)
        return {"error": error}


def not_found(what: str = "Drawing") -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} was not found.")
