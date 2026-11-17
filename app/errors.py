"""Typed application errors mapped to HTTP responses in ``app.api``."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ValidationError(ServiceError):
    status_code = 422
    code = "validation_error"


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class ContentMismatchError(ConflictError):
    code = "duplicate_serial_conflict"


class TaskInProgressError(ConflictError):
    code = "backfill_in_progress"
