"""Router /auth (docs/modules/auth/02-api-contracts.md).

POST /auth/apple (Sign in with Apple → выдать наш Bearer, 200),
GET /auth/tokens (список устройств, 200), DELETE /auth/tokens/{id} (revoke, 204).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.dependencies import CurrentToken, CurrentUser, SessionDep
from app.api.errors import (
    not_found,
    problem_responses,
    too_many_requests,
    unauthorized,
)
from app.auth import token_service
from app.auth.rate_limit import (
    check_login_rate_limit,
    check_user_login_lock,
    register_login_failure,
    reset_login_failures,
)
from app.schemas.api import (
    AppleSignInRequest,
    AppleSignInResponse,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
    SetSecretResponse,
    TokenOut,
    TokensListResponse,
)
from app.services.auth_service import (
    AppleTokenError,
    SecretAuthError,
    login_with_secret,
    register_with_secret,
    set_or_rotate_secret,
    sign_in_with_apple,
)

router = APIRouter(prefix="/auth", tags=["Аутентификация"])


@router.post(
    "/apple",
    response_model=AppleSignInResponse,
    status_code=status.HTTP_200_OK,
    summary="Вход через Apple",
    description=(
        "Принимает identity-токен Apple (Sign in with Apple) и возвращает Bearer-ключ "
        "для последующих запросов (формат `lv_<key_id>_<secret>`). Авторизация не требуется "
        "(это вход). При неуспешной проверке токена возвращается `401`. Частота входов "
        "ограничена по IP-адресу — при превышении возвращается `429`."
    ),
    responses=problem_responses(401, 429),
)
async def apple_sign_in(
    body: AppleSignInRequest,
    request: Request,
    session: SessionDep,
) -> AppleSignInResponse:
    """Возвращает Bearer-ключ по identity-токену Apple. Любой провал проверки → 401."""
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


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Регистрация по идентификатору и секрету",
    description=(
        "Создаёт новый аккаунт и возвращает идентификатор пользователя, секрет и Bearer-ключ "
        "(формат `lv_<key_id>_<secret>`). И идентификатор, и секрет генерируются сервером. "
        "Секрет показывается **один раз** — сохраните его для последующего входа с других "
        "устройств (`POST /auth/login`). Авторизация не требуется (это регистрация). Частота "
        "запросов ограничена по IP-адресу — при превышении возвращается `429`."
    ),
    responses=problem_responses(429),
)
async def register(
    body: RegisterRequest,
    request: Request,
    session: SessionDep,
) -> RegisterResponse:
    """Создаёт аккаунт (сервер генерирует идентификатор и секрет), выдаёт Bearer-ключ."""
    client_ip = request.client.host if request.client else "unknown"
    rl = await check_login_rate_limit(client_ip)
    if not rl.allowed:
        raise too_many_requests("Rate limit exceeded for sign-in.", retry_after_s=rl.retry_after_s)

    result = await register_with_secret(session, device_label=body.device_label)
    return RegisterResponse(
        user_id=result.user_id,
        secret=result.secret,
        api_key=result.api_key,
        token_id=result.token_id,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Вход по идентификатору и секрету",
    description=(
        "Принимает идентификатор пользователя и секрет, возвращает новый Bearer-ключ "
        "(формат `lv_<key_id>_<secret>`). Прежние ключи устройств не затрагиваются. "
        "Авторизация не требуется (это вход). При неуспешной проверке возвращается `401` "
        "без раскрытия причины. Частота входов ограничена по IP-адресу и по идентификатору "
        "пользователя — при превышении возвращается `429`."
    ),
    responses=problem_responses(401, 429),
)
async def login(
    body: LoginRequest,
    request: Request,
    session: SessionDep,
) -> LoginResponse:
    """Возвращает новый Bearer-ключ по идентификатору и секрету. Любой провал → единый 401."""
    client_ip = request.client.host if request.client else "unknown"
    ip_rl = await check_login_rate_limit(client_ip)
    if not ip_rl.allowed:
        raise too_many_requests(
            "Rate limit exceeded for sign-in.", retry_after_s=ip_rl.retry_after_s
        )

    # Per-user_id лок (defense-in-depth): гейт ДО проверки секрета. Ведётся по присланному
    # значению user_id независимо от существования юзера → не user-enumeration-оракул.
    user_lock = await check_user_login_lock(body.user_id)
    if not user_lock.allowed:
        raise too_many_requests(
            "Rate limit exceeded for sign-in.", retry_after_s=user_lock.retry_after_s
        )

    try:
        result = await login_with_secret(
            session,
            user_id=body.user_id,
            secret=body.secret,
            device_label=body.device_label,
        )
    except SecretAuthError as exc:
        # Неудача засчитывается в per-user_id лок; единый 401 без раскрытия причины.
        await register_login_failure(body.user_id)
        raise unauthorized("Invalid or expired credentials.") from exc

    # Успех → сброс per-user_id счётчика неудач.
    await reset_login_failures(body.user_id)
    return LoginResponse(
        api_key=result.api_key,
        token_id=result.token_id,
        user_id=result.user_id,
    )


@router.post(
    "/secret",
    response_model=SetSecretResponse,
    status_code=status.HTTP_200_OK,
    summary="Установить или сменить секрет",
    description=(
        "Устанавливает или меняет секрет текущего пользователя и возвращает новый секрет "
        "(показывается **один раз** — сохраните). Прежний секрет перестаёт действовать; "
        "ключи устройств не отзываются. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def set_secret(
    user: CurrentUser,
    session: SessionDep,
) -> SetSecretResponse:
    """Генерирует новый секрет текущего пользователя (set/rotate). Секрет — один раз."""
    result = await set_or_rotate_secret(session, user_id=user.id)
    return SetSecretResponse(user_id=result.user_id, secret=result.secret)


@router.get(
    "/tokens",
    response_model=TokensListResponse,
    summary="Список токенов устройств",
    description=(
        "Возвращает список активных токенов (устройств) текущего пользователя. У токена "
        "текущего запроса поле `current` равно `true`. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def list_tokens(
    user: CurrentUser,
    current_token: CurrentToken,
    session: SessionDep,
) -> TokensListResponse:
    """Возвращает активные токены (устройства) текущего пользователя."""
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


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отозвать токен устройства",
    description=(
        "Отзывает токен устройства (выход с одного устройства). Чужой или несуществующий "
        "токен → `404`. Операция идемпотентна. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def revoke_token(
    token_id: str,
    user: CurrentUser,
    _current_token: CurrentToken,
    session: SessionDep,
) -> Response:
    """Отзывает токен устройства. Чужой/несуществующий → 404. Идемпотентно."""
    ok = await token_service.revoke_token(session, user_id=user.id, token_id=token_id)
    if not ok:
        # Cross-tenant: не раскрываем существование чужого токена.
        raise not_found("Token not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
