"""Sign in with Apple: верификация → upsert user по apple_sub → выдача токена.

docs/modules/auth/03-architecture.md §1, ADR-007. API не делает ничего инлайн кроме
Postgres-операций; Apple-верификация изолирована в app.auth.apple_verify (мокается qa).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple_verify import AppleTokenError, verify_apple_identity_token
from app.auth.token_service import issue_token
from app.core.config import get_settings
from app.core.ids import new_token_secret, new_user_id
from app.core.logging import get_logger
from app.core.security import DUMMY_ARGON2_HASH, hash_api_key, verify_api_key
from app.db.models import User

logger = get_logger(__name__)


class SecretAuthError(Exception):
    """Любой провал входа по секрету на /auth/login (нет юзера / auth_secret_hash IS NULL /
    неверный секрет). Роутер транслирует в ЕДИНЫЙ 401 без раскрытия причины (ADR-024)."""


@dataclass(frozen=True)
class AppleSignInResult:
    api_key: str
    token_id: str
    user_id: str


@dataclass(frozen=True)
class RegisterResult:
    user_id: str
    secret: str
    api_key: str
    token_id: str


@dataclass(frozen=True)
class LoginResult:
    api_key: str
    token_id: str
    user_id: str


@dataclass(frozen=True)
class SetSecretResult:
    user_id: str
    secret: str


async def sign_in_with_apple(
    session: AsyncSession,
    *,
    identity_token: str,
    nonce: str | None,
    device_label: str | None,
) -> AppleSignInResult:
    """Верифицирует Apple identity token, upsert user по apple_sub, выдаёт наш Bearer.

    Любой провал верификации → AppleTokenError (роутер транслирует в 401, без деталей).
    """
    apple_sub = verify_apple_identity_token(identity_token, nonce=nonce)

    user = await _upsert_user_by_apple_sub(session, apple_sub)
    issued = await issue_token(session, user_id=user.id, device_label=device_label)
    await session.commit()

    logger.info("apple_sign_in", extra={"user_id": user.id, "key_id": issued.key_id})
    return AppleSignInResult(
        api_key=issued.api_key,
        token_id=issued.token_id,
        user_id=user.id,
    )


async def register_with_secret(
    session: AsyncSession,
    *,
    device_label: str | None,
) -> RegisterResult:
    """Регистрация нового аккаунта без Apple/админ-ключа (ADR-024 §1).

    Сервер генерирует И user_id, И секрет (клиентский user_id НЕ принимается — захват/
    коллизия). Создаёт users (apple_sub=NULL, adapty_customer_user_id=users.id, status=active)
    как admin-created (ADR-021 §B), пишет auth_secret_hash=argon2id(secret), выдаёт Bearer
    через token_service.issue_token(). Секрет возвращается ОДИН раз (не хранится/не восстановим).
    """
    settings = get_settings()
    secret = new_token_secret()
    user = User(
        id=new_user_id(),
        apple_sub=None,  # без Apple-якоря, как admin-created (ADR-021 §B; NULL вне UNIQUE)
        api_key_hash=None,
        auth_secret_hash=hash_api_key(secret),
        adapty_customer_user_id=None,
        monthly_budget_usd=Decimal(settings.user_monthly_budget_usd),
        status="active",
    )
    user.adapty_customer_user_id = user.id  # = users.id (как при Apple-входе).
    session.add(user)
    issued = await issue_token(session, user_id=user.id, device_label=device_label)
    await session.commit()

    # В лог — только user_id/key_id, НИКОГДА секрет (docs/05-security.md).
    logger.info("auth_register", extra={"user_id": user.id, "key_id": issued.key_id})
    return RegisterResult(
        user_id=user.id,
        secret=secret,
        api_key=issued.api_key,
        token_id=issued.token_id,
    )


async def login_with_secret(
    session: AsyncSession,
    *,
    user_id: str,
    secret: str,
    device_label: str | None,
) -> LoginResult:
    """Вход по user_id+секрет (ADR-024 §2). Выдаёт НОВЫЙ Bearer (мульти-устройство).

    Один SELECT по PK users.id + РОВНО ОДИН constant-time argon2.verify во ВСЕХ ветках:
    против реального auth_secret_hash, если юзер с секретом есть; иначе против
    DUMMY_ARGON2_HASH (нет юзера / auth_secret_hash IS NULL) — так латентность не зависит от
    существования user_id и не образует timing side-channel / user-enumeration-оракул
    (ADR-024 §4, docs/05-security.md). ЛЮБОЙ провал (нет юзера / auth_secret_hash IS NULL /
    неверный секрет) → SecretAuthError (роутер → единый 401 без раскрытия причины; не
    раскрываем существование user_id). Успех → новая строка api_tokens через
    token_service.issue_token().
    """
    user = await session.get(User, user_id)
    if user is None or user.auth_secret_hash is None:
        # Нет юзера / auth_secret_hash IS NULL: всё равно делаем ПОЛНОЦЕННЫЙ argon2.verify
        # против предвычисленного DUMMY_ARGON2_HASH (результат игнорируется), чтобы латентность
        # ответа не зависела от существования user_id/наличия секрета. Иначе ранний raise без
        # verify создал бы timing side-channel = user-enumeration-оракул (ADR-024 §4,
        # docs/05-security.md: «ровно один argon2.verify на запрос», неотличимость веток).
        verify_api_key(secret, DUMMY_ARGON2_HASH)
        raise SecretAuthError
    if not verify_api_key(secret, user.auth_secret_hash):
        raise SecretAuthError

    issued = await issue_token(session, user_id=user.id, device_label=device_label)
    await session.commit()

    logger.info("auth_login", extra={"user_id": user.id, "key_id": issued.key_id})
    return LoginResult(
        api_key=issued.api_key,
        token_id=issued.token_id,
        user_id=user.id,
    )


async def set_or_rotate_secret(session: AsyncSession, *, user_id: str) -> SetSecretResult:
    """Set/rotate секрета ТЕКУЩЕГО пользователя под Bearer (ADR-024 §5).

    Генерирует новый секрет, пишет auth_secret_hash=argon2id(secret) (set, если был NULL;
    rotate иначе — старый секрет инвалидируется). Существующие api_tokens НЕ отзываются
    (ротация секрета ≠ logout устройств). Секрет возвращается ОДИН раз.
    """
    user = await session.get(User, user_id)
    if user is None:  # current_user уже аутентифицирован; страховка от рассинхрона.
        raise SecretAuthError
    secret = new_token_secret()
    user.auth_secret_hash = hash_api_key(secret)
    await session.commit()

    logger.info("auth_secret_set", extra={"user_id": user.id})
    return SetSecretResult(user_id=user.id, secret=secret)


async def _upsert_user_by_apple_sub(session: AsyncSession, apple_sub: str) -> User:
    """Найти user по apple_sub (UNIQUE) либо создать нового (+ adapty_customer_user_id=id)."""
    existing = await _find_by_apple_sub(session, apple_sub)
    if existing is not None:
        return existing

    settings = get_settings()
    user = User(
        id=new_user_id(),
        apple_sub=apple_sub,
        api_key_hash=None,  # реальные токены — в api_tokens; legacy-поле не используется.
        # Маппинг user ↔ Adapty создаётся при первом входе iOS (= users.id), docs §auth.
        adapty_customer_user_id=None,
        monthly_budget_usd=Decimal(settings.user_monthly_budget_usd),
        status="active",
    )
    user.adapty_customer_user_id = user.id
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Гонка двух параллельных первых логинов одного apple_sub: UNIQUE отклонил второй.
        await session.rollback()
        winner = await _find_by_apple_sub(session, apple_sub)
        if winner is None:
            raise
        return winner
    return user


async def _find_by_apple_sub(session: AsyncSession, apple_sub: str) -> User | None:
    result = await session.execute(select(User).where(User.apple_sub == apple_sub))
    return result.scalar_one_or_none()


__all__ = [
    "AppleSignInResult",
    "AppleTokenError",
    "LoginResult",
    "RegisterResult",
    "SecretAuthError",
    "SetSecretResult",
    "login_with_secret",
    "register_with_secret",
    "set_or_rotate_secret",
    "sign_in_with_apple",
]
