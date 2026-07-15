from __future__ import annotations

from typing import Any


class LectureCastError(Exception):
    """A safe, structured error that can cross agent and CLI boundaries."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        next_action: str,
        retryable: bool = False,
        cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action
        self.retryable = retryable
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "next_action": self.next_action,
            "retryable": self.retryable,
        }
        if self.cause is not None:
            payload["cause"] = self.cause
        return payload

