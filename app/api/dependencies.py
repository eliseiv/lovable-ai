"""FastAPI-зависимости: Bearer-аутентификация (docs/modules/auth/03-architecture.md).

Sprint 3 (ADR-008, TD-004 closed): новый формат ключа lv_<key_id>_<secret> →
индексируемый O(1) lookup по api_tokens.key_id + ОДИН argon2-verify + rate-limit 60/min.
Legacy-путь (ключ без префикса lv_) → fallback на seeded users.api_key_hash (совместимость
S1/S2 на время миграции, ADR-008 «Миграционный путь»).
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import too_many_requests, unauthorized
from app.auth import token_service
from app.auth.rate_limit import check_key_rate_limit
from app.core.config import get_settings
from app.core.security import verify_api_key
from app.db.models import ApiToken, User
from app.db.session import get_session
from app.observability import sentry


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized()
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise unauthorized()
    return token


async def _authenticate_new_format(session: AsyncSession, request: Request, raw_key: str) -> User:
    """Новый путь lv_<key_id>_<secret>: rate-limit → O(1) lookup → один argon2-verify."""
    token = await token_service.authenticate(session, raw_key)
    if token is None:
        raise unauthorized()

    # Rate-limit 60/min на ключ (после успешной идентификации key_id, docs §5).
    rl = await check_key_rate_limit(token.key_id)
    if not rl.allowed:
        raise too_many_requests(
            "Rate limit exceeded (60 req/min per key).", retry_after_s=rl.retry_after_s
        )

    # Запоминаем токен текущего запроса для пометки current в GET /auth/tokens.
    request.state.current_token_id = token.id
    request.state.current_token_key_id = token.key_id

    user = await session.get(User, token.user_id)
    if user is None:
        raise unauthorized()
    # best-effort апдейт last_used_at (вне горячей транзакции аутентификации).
    await token_service.touch_last_used(session, token.id)
    return user


async def _authenticate_legacy(session: AsyncSession, raw_key: str) -> User:
    """Legacy fallback S1/S2: O(N)-перебор seeded users по api_key_hash (ADR-008)."""
    result = await session.execute(
        select(User).where(User.status == "active", User.api_key_hash.is_not(None))
    )
    for user in result.scalars().all():
        if user.api_key_hash is not None and verify_api_key(raw_key, user.api_key_hash):
            return user
    raise unauthorized()


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> User:
    """Authorization: Bearer <key> → current_user. Нет/невалиден → 401, превышение → 429.

    include_in_schema=False на заголовке: иначе FastAPI выводит Authorization явным
    header-параметром в КАЖДОМ аутентифицированном эндпоинте, дублируя глобальную кнопку
    Authorize (BearerAuth-схема, main.py). Скрытие из схемы оставляет только глобальный
    Authorize; dependency по-прежнему читает заголовок в рантайме (поведение не меняется).
    """
    raw_key = _extract_bearer(authorization)
    if token_service.is_new_format_key(raw_key):
        user = await _authenticate_new_format(session, request, raw_key)
    else:
        user = await _authenticate_legacy(session, raw_key)
    # Sprint 6 (ADR-015 §4): user_id → Sentry correlation-тег (НЕ Prometheus-label).
    # Проставляем тег в АКТИВНЫЙ изолированный scope запроса (открыт middleware `request_scope`)
    # ЗДЕСЬ — до тела эндпоинта, чтобы исключение текущего запроса несло тег user_id (тег после
    # обработки тегировал бы уже следующее событие). request.state — для прочих потребителей.
    request.state.user_id = user.id
    sentry.set_correlation(user_id=user.id)
    return user


async def get_current_token(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ApiToken:
    """Токен текущего запроса (только новый формат). Legacy-ключ не имеет строки api_tokens.

    Используется эндпоинтами /auth/tokens, где требуется адресовать сам токен. Legacy
    seeded-ключ S1 не имеет api_tokens-строки → 401 (управление токенами — только новый формат).
    """
    token_id = getattr(request.state, "current_token_id", None)
    if token_id is None:
        raise unauthorized()
    token = await session.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        raise unauthorized()
    return token


async def require_admin(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
) -> None:
    """Аутентификация админ-плоскости по заголовку X-Admin-Key (ADR-021 §A, docs/admin §1).

    Constant-time сравнение (hmac.compare_digest) с settings.admin_api_key. Пустой/None
    ADMIN_API_KEY → плоскость отключена (всегда 401; compare_digest против пустого никогда
    не проходит). Невалидно/нет заголовка → 401 RFC-7807 без раскрытия причины. Среда не
    гейтит — settings.environment не участвует (dev И prod).
    """
    settings = get_settings()
    configured = settings.admin_api_key
    provided = x_admin_key or ""
    # Пустой/None ключ или пустой заголовок → плоскость отключена/невалидно → 401.
    secret = configured.get_secret_value() if configured is not None else ""
    if not secret or not hmac.compare_digest(provided, secret):
        raise unauthorized("Invalid or missing admin credentials.")


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentToken = Annotated[ApiToken, Depends(get_current_token)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RequireAdmin = Annotated[None, Depends(require_admin)]
