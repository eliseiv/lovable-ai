"""Async httpx-клиент к Adapty Server-side API v2 + верификация подписи вебхука.

docs/02-tech-stack.md §Биллинг: клиент к Adapty — httpx (без отдельного SDK);
верификация подписи вебхука — штатными hmac/hashlib (stdlib) по ADAPTY_WEBHOOK_SECRET.
docs/modules/billing/03-architecture.md §2.1/§3, ADR-004/009.

- getProfile: GET профиля по customer_user_id для ресинка subscriptions.
- verify_webhook_signature: constant-time HMAC-SHA256 проверка подписи вебхука.
- Redis token-bucket rate-limit к Adapty API (ключ adapty:rl) — ресинк не превышает квоту.
- TLS verify включён (verify=True), таймауты заданы, 5xx/429 → AdaptyTransientError для
  backoff-ретрая (Celery), не валит весь батч ресинка.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import httpx
import redis.asyncio as aioredis

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Таймауты httpx к Adapty (connect/read/write/pool).
_HTTP_TIMEOUT_S = 10.0
# Redis token-bucket rate-limit к Adapty: фиксированное окно (INCR+EXPIRE), как в auth.
_ADAPTY_RL_KEY = "adapty:rl"
_ADAPTY_RL_WINDOW_S = 1
# Дефолтный потолок RPS к Adapty Server-side API (батч ресинка не превышает квоту).
_ADAPTY_RL_MAX_PER_WINDOW = 10


class AdaptyError(Exception):
    """Базовая ошибка взаимодействия с Adapty API."""


class AdaptyTransientError(AdaptyError):
    """Транзиентная ошибка (5xx/429/сетевой сбой/rate-limit) → backoff-ретрай, не валит батч."""


@dataclass(frozen=True)
class AdaptyProfile:
    """Нормализованный профиль Adapty (getProfile) для апдейта subscriptions."""

    access_level: str
    is_active: bool
    product_id: str | None
    store: str | None
    expires_at: str | None
    started_at: str | None
    will_renew: bool
    transaction_id: str | None
    raw: dict[str, Any]


def verify_webhook_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 проверка подписи вебхука Adapty (docs/05-security.md §Adapty).

    Подпись Adapty webhook v2 — HMAC-SHA256(secret, raw_body) в hex. Сравнение —
    constant-time (hmac.compare_digest). Пустой секрет/подпись → False (невалидно → 401).
    raw_body — СЫРОЕ тело запроса (до парсинга JSON), иначе подпись не сойдётся.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # Адапти может прислать подпись с префиксом схемы — сравниваем хвост hex как есть.
    candidate = signature.strip()
    return hmac.compare_digest(expected, candidate)


async def _check_rate_limit(redis_url: str) -> bool:
    """True, если запрос к Adapty в пределах RPS-лимита (Redis token-bucket, fixed-window)."""
    client = aioredis.from_url(redis_url)
    try:
        count = await client.incr(_ADAPTY_RL_KEY)
        if count == 1:
            await client.expire(_ADAPTY_RL_KEY, _ADAPTY_RL_WINDOW_S)
        return int(count) <= _ADAPTY_RL_MAX_PER_WINDOW
    finally:
        await client.aclose()  # type: ignore[attr-defined]  # redis 5.x async; stub устарел


class AdaptyClient:
    """httpx-клиент к Adapty Server-side API v2. Сетевой I/O изолирован (мокается qa)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        # Adapty Server-side API v2: авторизация секретным API-ключом.
        return {
            "Authorization": f"Api-Key {self._settings.adapty_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def get_profile(self, customer_user_id: str) -> AdaptyProfile | None:
        """getProfile по customer_user_id (= users.id). None, если профиль не найден (404).

        Rate-limit к Adapty (Redis token-bucket) проверяется перед вызовом. 5xx/429/
        сетевой сбой → AdaptyTransientError (backoff-ретрай). TLS verify включён.
        """
        if not await _check_rate_limit(self._settings.redis_url):
            raise AdaptyTransientError("Adapty API rate-limit reached (local token-bucket).")

        url = f"{self._settings.adapty_api_base}/server-side-api/profile/"
        params = {"customer_user_id": customer_user_id}
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=True) as client:
                resp = await client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise AdaptyTransientError(f"Adapty getProfile network error: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise AdaptyTransientError(f"Adapty getProfile transient status {resp.status_code}.")
        if resp.status_code != 200:
            raise AdaptyError(f"Adapty getProfile failed: status {resp.status_code}.")

        return _parse_profile(resp.json())


def _parse_profile(data: dict[str, Any]) -> AdaptyProfile:
    """Нормализует payload Adapty getProfile → AdaptyProfile.

    Точная схема Adapty фиксируется по актуальной доке; берём стабильный минимум:
    профиль с access_levels[<level>] (активный уровень) + subscriptions[*].
    """
    profile = data.get("data", data) if isinstance(data, dict) else {}
    access_levels = profile.get("access_levels", {}) or {}
    active_level_name: str | None = None
    active_level: dict[str, Any] = {}
    for name, level in access_levels.items():
        if isinstance(level, dict) and level.get("is_active"):
            active_level_name = name
            active_level = level
            break

    subscriptions = profile.get("subscriptions", {}) or {}
    sub: dict[str, Any] = {}
    if isinstance(subscriptions, dict) and subscriptions:
        sub = next(iter(subscriptions.values()), {}) or {}

    return AdaptyProfile(
        access_level=active_level_name or "free",
        is_active=bool(active_level.get("is_active", False)),
        product_id=active_level.get("vendor_product_id") or sub.get("vendor_product_id"),
        store=active_level.get("store") or sub.get("store"),
        expires_at=active_level.get("expires_at") or sub.get("expires_at"),
        started_at=active_level.get("starts_at") or sub.get("activated_at"),
        will_renew=bool(sub.get("will_renew", active_level.get("will_renew", False))),
        transaction_id=sub.get("vendor_transaction_id"),
        raw=data,
    )


def get_adapty_client() -> AdaptyClient:
    """Фабрика клиента (точка подмены для qa)."""
    return AdaptyClient(get_settings())
