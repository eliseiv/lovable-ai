"""Rate-limit: Redis token bucket по key_id (docs/05-security.md, modules/auth §5).

60 req/min на ключ (env RATE_LIMIT_PER_MIN). Гранулярность — токен (key_id): мульти-
устройство масштабируется независимо. Анонимный /auth/apple лимитируется по IP.
Превышение → 429 + Retry-After.

Алгоритм — фиксированное окно 60 с на атомарном INCR+EXPIRE (детерминированно,
без гонок). Token bucket с фиксированным refill 60 с эквивалентен fixed-window-счётчику
с лимитом = bucket size; этого достаточно для контракта «60/min на ключ».
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.core.logging import get_logger
from app.observability import metrics
from app.observability.redis_pool import get_redis

logger = get_logger(__name__)

_WINDOW_S = 60


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_s: int  # сек до сброса окна (для Retry-After), 0 если allowed


async def _check(redis_key: str, limit: int, *, scope: str) -> RateLimitResult:
    """Атомарный INCR с TTL окна. count==1 → ставим EXPIRE. count>limit → отказ.

    Sprint 6 (TD-007): соединение из переиспользуемого ConnectionPool (не per-request
    from_url/aclose). Отказ инкрементит lovable_rate_limit_rejected_total{scope}.
    Наблюдаемость занятых соединений — lovable_redis_pool_in_use{pool="rate_limit"}.
    """
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="rate_limit").inc()
    try:
        count = await client.incr(redis_key)
        if count == 1:
            await client.expire(redis_key, _WINDOW_S)
        if count > limit:
            ttl = await client.ttl(redis_key)
            retry_after = ttl if ttl and ttl > 0 else _WINDOW_S
            metrics.rate_limit_rejected_total.labels(scope=scope).inc()
            return RateLimitResult(allowed=False, retry_after_s=retry_after)
        return RateLimitResult(allowed=True, retry_after_s=0)
    finally:
        metrics.redis_pool_in_use.labels(pool="rate_limit").dec()


async def check_key_rate_limit(key_id: str) -> RateLimitResult:
    """Лимит 60/min на ключ (Redis-ключ rl:{key_id}). docs §5."""
    limit = get_settings().rate_limit_per_min
    return await _check(f"rl:{key_id}", limit, scope="api_key")


async def check_login_rate_limit(client_ip: str) -> RateLimitResult:
    """Лимит анонимного /auth/apple по IP (rl:apple:{ip}) — защита от брутфорса логина.

    Переиспользуется на публичных /auth/register и /auth/login (IP-лимит, ADR-024 §4).
    """
    limit = get_settings().rate_limit_per_min
    return await _check(f"rl:apple:{client_ip}", limit, scope="apple_login_ip")


def _user_lock_key(user_id: str) -> str:
    return f"rl:login:uid:{user_id}"


async def check_user_login_lock(user_id: str) -> RateLimitResult:
    """Per-`user_id` лок на /auth/login (defense-in-depth, ADR-024 §4): счётчик НЕудачных
    попыток входа на значение user_id уже превысил порог → 429 (без инкремента).

    Redis fixed-window `rl:login:uid:{user_id}`: порог LOGIN_USER_LOCK_THRESHOLD за окно
    LOGIN_USER_LOCK_WINDOW_S. Этот вызов — ГЕЙТ (до проверки секрета): только читает текущий
    счётчик, НЕ инкрементит (инкремент — register_login_failure при неудаче). Счётчик ведётся
    по присланному значению user_id НЕЗАВИСИМО от существования юзера → лок не становится
    user-enumeration-оракулом (docs/05-security §Клиентская аутентификация). docs §5A.
    """
    settings = get_settings()
    threshold = settings.login_user_lock_threshold
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="rate_limit").inc()
    try:
        raw = await client.get(_user_lock_key(user_id))
        count = int(raw) if raw is not None else 0
        if count >= threshold:
            ttl = await client.ttl(_user_lock_key(user_id))
            retry_after = ttl if ttl and ttl > 0 else settings.login_user_lock_window_s
            metrics.rate_limit_rejected_total.labels(scope="login_user_lock").inc()
            return RateLimitResult(allowed=False, retry_after_s=retry_after)
        return RateLimitResult(allowed=True, retry_after_s=0)
    finally:
        metrics.redis_pool_in_use.labels(pool="rate_limit").dec()


async def register_login_failure(user_id: str) -> None:
    """Инкремент счётчика НЕудачных попыток входа на значение user_id (ADR-024 §4).

    Атомарный INCR; на первой неудаче окна ставит EXPIRE = LOGIN_USER_LOCK_WINDOW_S
    (fixed-window). Вызывается ТОЛЬКО при провале верификации секрета на /auth/login.
    """
    settings = get_settings()
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="rate_limit").inc()
    try:
        count = await client.incr(_user_lock_key(user_id))
        if count == 1:
            await client.expire(_user_lock_key(user_id), settings.login_user_lock_window_s)
    finally:
        metrics.redis_pool_in_use.labels(pool="rate_limit").dec()


async def reset_login_failures(user_id: str) -> None:
    """Сброс счётчика неудач (DEL) при успешном входе на этот user_id (ADR-024 §4)."""
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="rate_limit").inc()
    try:
        await client.delete(_user_lock_key(user_id))
    finally:
        metrics.redis_pool_in_use.labels(pool="rate_limit").dec()
