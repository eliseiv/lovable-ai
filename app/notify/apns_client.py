"""APNs HTTP/2 клиент + provider-JWT ES256 (Sprint 5, ADR-013, docs/notify §3-4).

APNs Provider API работает ТОЛЬКО по HTTP/2 (httpx[http2]). Provider-auth — JWT ES256
(claims iss=APNS_TEAM_ID, kid=APNS_KEY_ID, iat), подписанный .p8-ключом (PyJWT[crypto]).
JWT кэшируется и переподписывается не чаще APNS_JWT_TTL_S (Apple отвергает частую
регенерацию как too-many-token-updates).

.p8-ключ — секрет/конфиг-артефакт: содержимое APNS_AUTH_KEY (SecretStr) приоритетнее,
иначе файл APNS_AUTH_KEY_PATH. В логах apns_token маскируется (last 6), .p8/JWT никогда
не логируются (docs/05-security.md §APNs).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

from app.core.config import Settings
from app.core.logging import get_logger
from app.observability import metrics

logger = get_logger(__name__)

_APNS_HOST_PRODUCTION = "api.push.apple.com"
_APNS_HOST_SANDBOX = "api.sandbox.push.apple.com"
_APNS_ALGORITHM = "ES256"
# Таймаут одного APNs-запроса (сек) — внешний HTTP/2 к Apple.
_REQUEST_TIMEOUT_S = 10.0


class ApnsConfigError(Exception):
    """APNs не сконфигурирован (нет .p8-ключа/key_id/team_id) — push no-op (ADR-013 §5)."""


class ApnsTransientError(Exception):
    """Транзиентный сбой APNs (429/5xx) — Celery retry с backoff (ADR-006/ADR-013)."""


@dataclass(frozen=True)
class ApnsResult:
    """Результат отправки на одно устройство."""

    ok: bool
    # True → токен мёртв (410 Unregistered / 400 BadDeviceToken) → invalidated_at=now().
    invalid_token: bool
    status_code: int
    detail: str


def _apns_status_label(status_code: int) -> str:
    """Сводит HTTP-статус APNs к ограниченному label apns_status (200/410/400/429/5xx).

    5xx сворачивается в один bucket (запрет unbounded-кардинальности, ADR-015 §1); прочие
    статусы — точные строки из нормативной таблицы (§2.4); иное → текстовый код статуса.
    """
    if status_code >= 500:
        return "5xx"
    if status_code in (200, 400, 410, 429):
        return str(status_code)
    return str(status_code)


def _mask_token(apns_token: str) -> str:
    """Маскирует apns_token для логов (last 6, остальное звёздочки)."""
    if len(apns_token) <= 6:
        return "*" * len(apns_token)
    return "*" * (len(apns_token) - 6) + apns_token[-6:]


def _load_p8_key(settings: Settings) -> str:
    """Содержимое .p8-ключа: APNS_AUTH_KEY (PEM-строка) приоритетнее, иначе файл по пути.

    Нет ни того, ни другого → ApnsConfigError (push no-op). Файл читается на стороне
    воркера (provision — devops/secret-mount, не в git).
    """
    if settings.apns_auth_key is not None:
        pem = settings.apns_auth_key.get_secret_value()
        if pem:
            return pem
    if settings.apns_auth_key_path:
        path = Path(settings.apns_auth_key_path)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        raise ApnsConfigError(f"APNS_AUTH_KEY_PATH not found: {settings.apns_auth_key_path}")
    raise ApnsConfigError("APNs auth key not configured (APNS_AUTH_KEY / APNS_AUTH_KEY_PATH).")


class _ProviderTokenCache:
    """Кэш provider-JWT ES256: переподпись не чаще APNS_JWT_TTL_S (ADR-013 §4).

    Один экземпляр на процесс воркера (как auth/rate-limit паттерн). Apple допускает реюз
    JWT до ~1 ч; повторная генерация на каждый push отвергается как too-many-token-updates.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._issued_at: float = 0.0

    def get(self, settings: Settings) -> str:
        """Кэшированный/переподписанный provider-JWT. ApnsConfigError, если нет credentials."""
        now = time.monotonic()
        if self._token is not None and (now - self._issued_at) < settings.apns_jwt_ttl_s:
            return self._token
        pem = _load_p8_key(settings)
        token = jwt.encode(
            {"iss": settings.apns_team_id, "iat": int(time.time())},
            pem,
            algorithm=_APNS_ALGORITHM,
            headers={"kid": settings.apns_key_id, "alg": _APNS_ALGORITHM},
        )
        self._token = token
        self._issued_at = now
        return token


# Синглтон кэша JWT (живёт между push в рамках процесса воркера).
_token_cache = _ProviderTokenCache()


def get_token_cache() -> _ProviderTokenCache:
    """Точка доступа/подмены кэша provider-JWT (qa мокает get)."""
    return _token_cache


def _apns_host(settings: Settings, device_environment: str) -> str:
    """APNs-хост по device_tokens.environment (override-дефолт APNS_ENV)."""
    env = device_environment or settings.apns_env
    return _APNS_HOST_PRODUCTION if env == "production" else _APNS_HOST_SANDBOX


def build_payload(to_state: str, job_id: str, live_url: str | None) -> dict[str, object]:
    """APNs-payload: aps.alert (локализуемый ключ под to_state) + custom job_id/state/live_url.

    Клиент локализует по ключу alert (docs/notify out-of-scope: сервер шлёт стабильный ключ
    + данные для deep-link). live_url включается для LIVE (deep-link на готовый сайт).
    """
    custom: dict[str, object] = {"job_id": job_id, "state": to_state}
    if live_url is not None:
        custom["live_url"] = live_url
    return {
        "aps": {
            "alert": {"loc-key": f"job_status_{to_state.lower()}"},
            "sound": "default",
        },
        **custom,
    }


class ApnsClient:
    """HTTP/2 клиент к APNs Provider API (один на процесс воркера, переиспользует пул)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(
        self,
        *,
        apns_token: str,
        device_environment: str,
        payload: dict[str, object],
    ) -> ApnsResult:
        """HTTP/2 POST /3/device/{token}. 200 → ok; 410/400 BadDeviceToken → invalid_token;
        429/5xx → ApnsTransientError (Celery retry). Прочие 4xx → ok=False (best-effort drop).
        """
        settings = self._settings
        host = _apns_host(settings, device_environment)
        url = f"https://{host}/3/device/{apns_token}"
        provider_jwt = get_token_cache().get(settings)
        headers = {
            "authorization": f"bearer {provider_jwt}",
            "apns-topic": settings.apns_bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        masked = _mask_token(apns_token)
        request_started = time.monotonic()
        async with httpx.AsyncClient(http2=True, timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload, headers=headers)
        # Sprint 6 (ADR-015 §2.4): latency HTTP/2-запроса к APNs.
        metrics.apns_request_latency_seconds.observe(time.monotonic() - request_started)
        status_label = _apns_status_label(resp.status_code)

        if resp.status_code == 200:
            metrics.apns_push_total.labels(result="delivered", apns_status="200").inc()
            return ApnsResult(ok=True, invalid_token=False, status_code=200, detail="ok")

        reason = _extract_reason(resp)
        # 410 Unregistered / 400 BadDeviceToken → мёртвый токен (инвалидация).
        if resp.status_code == 410 or (resp.status_code == 400 and reason == "BadDeviceToken"):
            logger.info(
                "apns_token_invalid",
                extra={"apns_token": masked, "status": resp.status_code, "reason": reason},
            )
            metrics.apns_push_total.labels(result="invalidated", apns_status=status_label).inc()
            metrics.apns_tokens_invalidated_total.labels(
                reason="unregistered_410" if resp.status_code == 410 else "bad_token_400"
            ).inc()
            return ApnsResult(
                ok=False, invalid_token=True, status_code=resp.status_code, detail=reason
            )
        # 429 / 5xx → транзиентно → Celery retry (ADR-006).
        if resp.status_code == 429 or resp.status_code >= 500:
            metrics.apns_push_total.labels(result="retry", apns_status=status_label).inc()
            raise ApnsTransientError(f"APNs transient {resp.status_code}: {reason}")
        # Прочие 4xx — best-effort drop (не блокирует пайплайн).
        logger.warning(
            "apns_send_failed",
            extra={"apns_token": masked, "status": resp.status_code, "reason": reason},
        )
        metrics.apns_push_total.labels(result="drop", apns_status=status_label).inc()
        return ApnsResult(
            ok=False, invalid_token=False, status_code=resp.status_code, detail=reason
        )


def _extract_reason(resp: httpx.Response) -> str:
    """APNs reason-код из тела ответа (JSON {reason}). Пусто/не-JSON → текст статуса."""
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError):
        return resp.reason_phrase or str(resp.status_code)
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else (resp.reason_phrase or str(resp.status_code))
