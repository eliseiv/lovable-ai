"""Router /auth (docs/modules/auth/02-api-contracts.md).

POST /auth/apple (Sign in with Apple → выдать наш Bearer, 200),
GET /auth/tokens (список устройств, 200), DELETE /auth/tokens/{id} (revoke, 204).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.dependencies import CurrentToken, CurrentUser, SessionDep
from app.api.errors import not_found, too_many_requests, unauthorized
from app.auth import token_service
from app.auth.rate_limit import check_login_rate_limit
from app.schemas.api import (
    AppleSignInRequest,
    AppleSignInResponse,
    TokenOut,
    TokensListResponse,
)
from app.services.auth_service import AppleTokenError, sign_in_with_apple

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/apple", response_model=AppleSignInResponse, status_code=status.HTTP_200_OK)
async def apple_sign_in(
    body: AppleSignInRequest,
    request: Request,
    session: SessionDep,
) -> AppleSignInResponse:
    """Sign in with Apple. НЕ Bearer (это логин). Любой провал верификации → 401.

    Анонимный эндпоинт лимитируется по IP (rl:apple:{ip}) — защита от брутфорса логина.
    """
    client_ip = request.client.host if request.client else "unknown"
    rl = await check_login_rate_limit(client_ip)
    if not rl.allowed:
        raise too_many_requests("Rate limit exceeded for sign-in.", retry_after_s=rl.retry_after_s)

    try:
        result = await sign_in_with_apple(
            session,
            identity_token=body.identity_token,
            nonce=body.nonce,
            device_label=body.device_label,
        )
    except AppleTokenError as exc:
        # Не раскрываем какую именно проверку не прошёл Apple-токен (docs §02-api-contracts).
        raise unauthorized("Invalid or expired credentials.") from exc

    return AppleSignInResponse(
        api_key=result.api_key,
        token_id=result.token_id,
        user_id=result.user_id,
    )


@router.get("/tokens", response_model=TokensListResponse)
async def list_tokens(
    user: CurrentUser,
    current_token: CurrentToken,
    session: SessionDep,
) -> TokensListResponse:
    """Список активных токенов (устройств) текущего пользователя. current — текущий запрос."""
    tokens = await token_service.list_active_tokens(session, user.id)
    out = [
        TokenOut(
            id=t.id,
            key_id=t.key_id,
            device_label=t.device_label,
            created_at=t.created_at,
            last_used_at=t.last_used_at,
            current=(t.id == current_token.id),
        )
        for t in tokens
    ]
    return TokensListResponse(tokens=out)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: str,
    user: CurrentUser,
    _current_token: CurrentToken,
    session: SessionDep,
) -> Response:
    """Отзыв токена (logout одного устройства). Чужой/несуществующий → 404. Идемпотентно."""
    ok = await token_service.revoke_token(session, user_id=user.id, token_id=token_id)
    if not ok:
        # Cross-tenant: не раскрываем существование чужого токена.
        raise not_found("Token not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
