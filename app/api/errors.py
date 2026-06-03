"""RFC-7807 (application/problem+json) ошибки (docs/modules/api/02-api-contracts.md)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.api import Problem

_BASE = "https://api.domain/errors"

# Описания HTTP-кодов ошибок для публичной OpenAPI-схемы (api-contracts §B.3). Русские,
# в формате RFC 7807 (модель Problem). Применяются к эндпоинтам через `responses=`.
_PROBLEM_CONTENT = {"application/problem+json": {"schema": Problem.model_json_schema()}}
_ERROR_DETAIL: dict[int, str] = {
    401: "Не пройдена авторизация (отсутствует или недействителен ключ).",
    402: "Нет активной подписки или исчерпана квота.",
    404: "Ресурс не найден.",
    409: "Конфликт: операция невозможна в текущем состоянии.",
    422: "Некорректные данные запроса.",
    429: "Превышен лимит частоты запросов.",
}


def problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Словарь OpenAPI-`responses` для перечисленных кодов ошибок (модель RFC 7807).

    Используется в декораторах роутов (`responses=problem_responses(401, 404, ...)`), чтобы
    публичная схема документировала ошибочные ответы с моделью Problem (media-type
    `application/problem+json`) и русским описанием.
    """
    return {
        code: {
            "description": _ERROR_DETAIL.get(code, "Ошибка."),
            "content": _PROBLEM_CONTENT,
        }
        for code in codes
    }


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


def _validation_error_items(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Поля-ошибки из RequestValidationError в JSON-сериализуемом виде (RFC-7807 errors[]).

    `loc` приводим к строке (точечный путь поля); `ctx` отбрасываем — он может содержать
    несериализуемые объекты (исключения Pydantic) и не нужен клиенту iOS.
    """
    items: list[dict[str, Any]] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        items.append({"loc": loc, "msg": str(err.get("msg", "")), "type": str(err.get("type", ""))})
    return items


async def validation_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """App-level обработчик RequestValidationError → application/problem+json (RFC-7807, 422).

    Нормативный контракт (docs/modules/api/03 → Обработчики ошибок; auth/02 §Ошибки): ВСЕ 422,
    включая публичный `POST /auth/apple` без `identity_token`, обязаны нести
    `application/problem+json`, а не дефолтный FastAPI `{detail:[...]}` (application/json).
    Регистрируется на `FastAPI(...)`-инстансе (app-level) — единая точка для всех роутеров,
    исключает «забытые» эндпоинты. Прочие 422 (например `unprocessable()` → ProblemException)
    идут своим путём и не затрагиваются.

    `detail` агрегирует поля-ошибки в человекочитаемую строку (RFC-7807 detail); полный список —
    в доменном поле `errors[]` (точечный `loc` + `msg` + `type`).
    """
    assert isinstance(exc, RequestValidationError)
    errors = _validation_error_items(exc)
    detail = "; ".join(f"{e['loc']}: {e['msg']}" for e in errors) or "Некорректные данные запроса."
    body: dict[str, Any] = {
        "type": f"{_BASE}/unprocessable-entity",
        "title": "Unprocessable Entity",
        "status": 422,
        "detail": detail,
        "errors": errors,
    }
    return JSONResponse(
        status_code=422,
        content=body,
        media_type="application/problem+json",
    )


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
