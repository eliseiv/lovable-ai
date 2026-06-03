"""Токен-сервис: lv_<key_id>_<secret>, индексируемый O(1) lookup (ADR-008, TD-004).

Выдача/чтение/revoke api_tokens. В БД — только публичный key_id + argon2id-хэш секрета.
Аутентификация: парс key_id → один SELECT по UNIQUE key_id → один constant-time
argon2-verify секрета. Заменяет O(N)-перебор S1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_key_id, new_token_id, new_token_secret
from app.core.logging import get_logger
from app.core.security import hash_api_key, verify_api_key
from app.db.models import ApiToken

logger = get_logger(__name__)

_KEY_PREFIX = "lv_"


@dataclass(frozen=True)
class IssuedToken:
    """Результат выдачи токена. `api_key` отдаётся клиенту ЕДИНСТВЕННЫЙ раз."""

    token_id: str
    key_id: str
    api_key: str  # полный lv_<key_id>_<secret>


@dataclass(frozen=True)
class ParsedKey:
    key_id: str
    secret: str


def parse_api_key(raw: str) -> ParsedKey | None:
    """Парсит `lv_<key_id>_<secret>` → (key_id, secret). None при отсутствии префикса/формата.

    key_id — [a-z0-9]{16} (без `_`); secret (token_urlsafe) может содержать `_`/`-`,
    поэтому режем строго по первым двум `_`: lv, key_id, secret(остаток).
    """
    if not raw.startswith(_KEY_PREFIX):
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3:
        return None
    _, key_id, secret = parts
    if not key_id or not secret:
        return None
    return ParsedKey(key_id=key_id, secret=secret)


def is_new_format_key(raw: str) -> bool:
    """True для нового формата lv_… (новый путь lookup), False → legacy fallback S1."""
    return raw.startswith(_KEY_PREFIX)


async def issue_token(
    session: AsyncSession,
    *,
    user_id: str,
    device_label: str | None = None,
) -> IssuedToken:
    """Генерирует key_id+secret, пишет строку api_tokens (key_hash=argon2id(secret)).

    Коммит — на стороне вызывающего. Возвращает полный ключ для разовой выдачи клиенту.
    """
    key_id = new_key_id()
    secret = new_token_secret()
    token = ApiToken(
        id=new_token_id(),
        user_id=user_id,
        key_id=key_id,
        key_hash=hash_api_key(secret),
        device_label=device_label,
    )
    session.add(token)
    # В лог — только key_id, НИКОГДА secret (docs/05-security.md).
    logger.info("api_token_issued", extra={"user_id": user_id, "key_id": key_id})
    return IssuedToken(
        token_id=token.id,
        key_id=key_id,
        api_key=f"{_KEY_PREFIX}{key_id}_{secret}",
    )


async def authenticate(session: AsyncSession, raw_key: str) -> ApiToken | None:
    """Индексируемый O(1) lookup: key_id → одна строка → один argon2-verify (ADR-008).

    None при любом провале (нет строки / verify fail / отозван / неверный формат).
    Число argon2-verify не зависит от числа юзеров/токенов (TD-004 closed).
    """
    parsed = parse_api_key(raw_key)
    if parsed is None:
        return None
    result = await session.execute(
        select(ApiToken).where(
            ApiToken.key_id == parsed.key_id,
            ApiToken.revoked_at.is_(None),
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        return None
    # Ровно один constant-time argon2-verify секрета.
    if not verify_api_key(parsed.secret, token.key_hash):
        return None
    return token


async def touch_last_used(session: AsyncSession, token_id: str) -> None:
    """Best-effort апдейт last_used_at (UI/аудит). Вне горячей транзакции аутентификации."""
    await session.execute(
        update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=datetime.now(UTC))
    )
    await session.commit()


async def list_active_tokens(session: AsyncSession, user_id: str) -> list[ApiToken]:
    """Активные токены (устройства) пользователя: revoked_at IS NULL."""
    result = await session.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_token(session: AsyncSession, *, user_id: str, token_id: str) -> bool:
    """Мягкий revoke (revoked_at = now()) токена, принадлежащего user_id.

    Возвращает True, если токен найден и принадлежит user_id (включая уже отозванный —
    идемпотентность). False → чужой/несуществующий токен (вызывающий отдаёт 404).
    Строка сохраняется для аудита.
    """
    result = await session.execute(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user_id)
    )
    token = result.scalar_one_or_none()
    if token is None:
        # Чужой или несуществующий — не раскрываем существование (cross-tenant → 404).
        return False
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await session.commit()
        logger.info("api_token_revoked", extra={"user_id": user_id, "key_id": token.key_id})
    return True
