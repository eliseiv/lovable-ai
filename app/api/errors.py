"""RFC-7807 (application/problem+json) ошибки (docs/modules/api/02-api-contracts.md)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

_BASE = "https://api.domain/errors"


class ProblemException(Exception):
    """Исключение, сериализуемое в application/problem+json."""

    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        problem_type: str,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.problem_type = problem_type
        self.extra = extra or {}
        self.headers = headers or {}
        super().__init__(detail)


def problem_response(exc: ProblemException) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{_BASE}/{exc.problem_type}",
        "title": exc.title,
        "status": exc.status,
        "detail": exc.detail,
    }
    body.update(exc.extra)
    return JSONResponse(
        status_code=exc.status,
        content=body,
        media_type="application/problem+json",
        headers=exc.headers or None,
    )


async def problem_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Зарегистрирован только для ProblemException; сигнатура совместима со Starlette.
    assert isinstance(exc, ProblemException)
    return problem_response(exc)


def unauthorized(detail: str = "Invalid or missing API key.") -> ProblemException:
    return ProblemException(
        status=401, title="Unauthorized", detail=detail, problem_type="unauthorized"
    )


def not_found(detail: str = "Resource not found.") -> ProblemException:
    return ProblemException(status=404, title="Not Found", detail=detail, problem_type="not-found")


def conflict(detail: str, current_state: str | None = None) -> ProblemException:
    extra = {"current_state": current_state} if current_state else None
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type="conflict", extra=extra
    )


def unprocessable(detail: str) -> ProblemException:
    return ProblemException(
        status=422,
        title="Unprocessable Entity",
        detail=detail,
        problem_type="unprocessable-entity",
    )


def payment_required(
    detail: str, *, reason: str, required_entitlement: str | None = None
) -> ProblemException:
    """402 Payment Required (RFC-7807) — quota-gate billing (docs/modules/billing/02-api §3).

    reason ∈ no_entitlement / quota_exhausted / project_limit / concurrency_limit.
    required_entitlement — минимальный access_level, снимающий ограничение (iOS → Adapty-пейвол).
    """
    extra: dict[str, Any] = {"reason": reason}
    if required_entitlement is not None:
        extra["required_entitlement"] = required_entitlement
    return ProblemException(
        status=402,
        title="Payment Required",
        detail=detail,
        problem_type="payment-required",
        extra=extra,
    )


def too_many_requests(detail: str, *, retry_after_s: int | None = None) -> ProblemException:
    """429 (RFC-7807) с опц. Retry-After (docs/05-security.md §Rate-limit/concurrency)."""
    extra = {"retry_after": retry_after_s} if retry_after_s is not None else None
    headers = {"Retry-After": str(retry_after_s)} if retry_after_s is not None else None
    return ProblemException(
        status=429,
        title="Too Many Requests",
        detail=detail,
        problem_type="too-many-requests",
        extra=extra,
        headers=headers,
    )
