"""Верификация Apple identity token по JWKS (docs/modules/auth/03-architecture.md §1, ADR-007).

Server-side проверка: подпись по публичным ключам Apple (JWKS, кэш по kid),
iss == https://appleid.apple.com, aud == APPLE_AUDIENCE, exp/iat/nbf, nonce.

Изоляция для тестов: сетевой доступ к JWKS вынесен в `_AppleJwksClient.fetch_jwks`,
который qa мокает (без реального вызова Apple). `verify_apple_identity_token` —
чистая верификация над переданным набором ключей.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from jwt import PyJWK, PyJWKSet
from jwt.exceptions import InvalidTokenError

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_ALGORITHMS = ["RS256"]
# Допуск на рассинхрон часов (сек) при проверке exp/iat/nbf.
_LEEWAY_S = 30
# TTL кэша JWKS (сек). Apple ротирует ключи редко; refresh при неизвестном kid.
_JWKS_CACHE_TTL_S = 3600


class AppleTokenError(Exception):
    """Любой провал верификации Apple identity token (наружу → 401, без деталей)."""


class _AppleJwksClient:
    """Кэширующий клиент JWKS Apple. Сеть изолирована в `fetch_jwks` (мокается в тестах)."""

    def __init__(self) -> None:
        self._cached_set: PyJWKSet | None = None
        self._fetched_at: float = 0.0

    def fetch_jwks(self) -> dict[str, Any]:
        """Сетевой вызов JWKS Apple. ЕДИНСТВЕННАЯ точка I/O — мокается qa.

        TLS verify включён (verify=True по умолчанию), таймаут задан.
        """
        settings = get_settings()
        resp = httpx.get(settings.apple_jwks_url, timeout=10.0)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def _refresh(self) -> None:
        raw = self.fetch_jwks()
        self._cached_set = PyJWKSet.from_dict(raw)
        self._fetched_at = time.monotonic()

    def get_signing_key(self, kid: str) -> PyJWK:
        """Ключ по kid из кэша; при неизвестном kid / протухшем кэше — refresh JWKS."""
        if self._cached_set is None or (time.monotonic() - self._fetched_at) > _JWKS_CACHE_TTL_S:
            self._refresh()
        key = self._lookup(kid)
        if key is None:
            # Неизвестный kid — Apple мог ротировать ключи: один принудительный refresh.
            self._refresh()
            key = self._lookup(kid)
        if key is None:
            raise AppleTokenError("Signing key not found for kid.")
        return key

    def _lookup(self, kid: str) -> PyJWK | None:
        if self._cached_set is None:
            return None
        for key in self._cached_set.keys:
            if key.key_id == kid:
                return key
        return None


# Синглтон клиента (кэш JWKS живёт между запросами в рамках процесса).
_jwks_client = _AppleJwksClient()


def get_jwks_client() -> _AppleJwksClient:
    """Точка доступа/подмены JWKS-клиента (qa мокает get_signing_key/fetch_jwks)."""
    return _jwks_client


def verify_apple_identity_token(identity_token: str, *, nonce: str | None = None) -> str:
    """Верифицирует Apple identity token, возвращает `sub` (apple_sub). Провал → AppleTokenError.

    Проверки (любой провал → AppleTokenError, наружу 401 без раскрытия конкретной):
    подпись (JWKS Apple по kid), iss, aud == APPLE_AUDIENCE, exp/iat/nbf, nonce (если передан).
    """
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(identity_token)
    except InvalidTokenError as exc:
        raise AppleTokenError("Malformed identity token header.") from exc

    kid = header.get("kid")
    if not kid:
        raise AppleTokenError("Missing kid in token header.")

    signing_key = get_jwks_client().get_signing_key(kid)

    try:
        claims: dict[str, Any] = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=settings.apple_audience,
            issuer=settings.apple_issuer,
            leeway=_LEEWAY_S,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except InvalidTokenError as exc:
        # Не раскрываем какую проверку не прошёл (подпись/aud/iss/exp) — единый 401.
        raise AppleTokenError("Identity token verification failed.") from exc

    if nonce is not None:
        token_nonce = claims.get("nonce")
        if token_nonce != nonce:
            raise AppleTokenError("Nonce mismatch.")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AppleTokenError("Missing sub claim.")
    return sub
