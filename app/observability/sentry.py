"""Sentry init + scrubbing секретов (Sprint 6, ADR-015, docs observability §4 / 05-security).

Инструментация исключений для FastAPI (ASGI/Starlette) и Celery (CeleryIntegration).
Пустой SENTRY_DSN → init no-op (фича неактивна, процесс цел — как APNs без credentials).

Scrubbing (нормативно, before_send-hook + send_default_pii=False): из событий Sentry
НИКОГДА не утекают значения секретов (single normative source — 05-security → Секреты):
  - ANTHROPIC_API_KEY/ADAPTY_API_KEY/ADAPTY_WEBHOOK_SECRET/SEED_API_KEY/S3_ACCESS_KEY/
    S3_SECRET_KEY, APNs .p8 (APNS_AUTH_KEY)+provider-JWT, Apple identity token, DSN-пароли;
  - Bearer-ключ lv_<key_id>_<secret> — допустим ТОЛЬКО key_id, секретная часть вырезается;
  - apns_token маскируется (last 6).

Реализация — denylist ключей по имени + regex на token-паттерны (lv_/Bearer/PEM).
Correlation job_id/project_id/user_id — через sentry_sdk.set_tag (api middleware / Celery-
обёртка); единственное место, где высококардинальные идентификаторы в observability.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_REDACTED = "[REDACTED]"

# Denylist по ИМЕНИ ключа (case-insensitive substring). Любое значение под таким ключом в
# event (extra/contexts/request data/vars) вырезается. Список = секреты 05-security → Секреты.
_SECRET_KEY_TOKENS: tuple[str, ...] = (
    "anthropic_api_key",
    "adapty_api_key",
    "adapty_webhook_secret",
    "seed_api_key",
    "s3_access_key",
    "s3_secret_key",
    "apns_auth_key",
    "sentry_dsn",
    "password",
    "secret",
    "authorization",
    "api_key",
    "private_key",
    "identity_token",
    "id_token",
    "access_token",
    "provider_jwt",
)

# Regex token-паттернов в произвольных строковых значениях (даже если ключ не в denylist):
#   - наш Bearer-ключ lv_<key_id>_<secret> → оставляем только lv_<key_id> (секрет вырезаем);
#   - заголовок Bearer <token> → Bearer [REDACTED];
#   - PEM-блоки (.p8 содержимое / приватные ключи) → [REDACTED PEM];
#   - apns_token (длинный hex device-token) → маскируется (last 6).
_LV_KEY_RE = re.compile(r"(lv_[A-Za-z0-9]+)_[A-Za-z0-9]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+")
_PEM_RE = re.compile(
    r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----",
    re.DOTALL,
)
_APNS_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def _scrub_string(value: str) -> str:
    """Применяет regex-маскировку token-паттернов к строке (Bearer/lv_/PEM/apns_token)."""
    masked = _PEM_RE.sub("[REDACTED PEM]", value)
    masked = _LV_KEY_RE.sub(r"\1_" + _REDACTED, masked)
    masked = _BEARER_RE.sub(r"\1 " + _REDACTED, masked)
    return _APNS_TOKEN_RE.sub(lambda m: _mask_hex(m.group(0)), masked)


def scrub_text(value: str) -> str:
    """Публичный scrubber строки: маскирует token-паттерны (Bearer/lv_/PEM/apns_token), §4.

    Переиспользуется вне Sentry-пути — например, для усечённого сырого ответа модели в
    диагностике structured-output (ADR-020 §I.4): сырой текст не должен утечь с секретами в
    логи/job_events. Единый нормативный набор паттернов (05-security → Секреты)."""
    return _scrub_string(value)


def _mask_hex(token: str) -> str:
    """Маскирует длинный hex-токен (apns_token и пр.): last 6, остальное звёздочки."""
    if len(token) <= 6:
        return "*" * len(token)
    return "*" * (len(token) - 6) + token[-6:]


def _key_is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(tok in lowered for tok in _SECRET_KEY_TOKENS)


def _scrub(obj: Any, *, key: str | None = None) -> Any:
    """Рекурсивно вырезает секреты из event-структуры (dict/list/str).

    - значение под секретным КЛЮЧОМ (denylist) → [REDACTED];
    - произвольная строка → regex-маскировка token-паттернов;
    - dict/list — рекурсия.
    """
    if key is not None and _key_is_secret(key) and isinstance(obj, str):
        return _REDACTED
    if isinstance(obj, dict):
        return {k: _scrub(v, key=str(k)) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj)
    return obj


def before_send(event: Any, _hint: Any = None) -> Any:
    """Sentry before_send-hook: scrubbing секретов из исходящего event (нормативно §4).

    Никогда не должен сам бросать исключение (иначе Sentry уронит отправку и/или поглотит
    событие без скраба) — при любой ошибке скраба возвращаем безопасную заглушку, не
    исходное событие с секретами. Сигнатура — (event, hint), как требует sentry_sdk.
    """
    try:
        scrubbed = _scrub(event)
        # _scrub гарантированно сохраняет тип верхнего уровня (dict).
        return scrubbed if isinstance(scrubbed, dict) else event
    except Exception:  # noqa: BLE001 — скраб не должен падать; лучше пустой event, чем утечка
        logger.warning("sentry_scrub_failed")
        return {"message": "[scrub failed: event suppressed]", "level": "error"}


def init_sentry(settings: Settings | None = None) -> bool:
    """Инициализирует Sentry для FastAPI + Celery (ADR-015 §4). Пустой DSN → no-op.

    Возвращает True, если Sentry инициализирован (DSN задан), False — no-op.
    Идемпотентность обеспечивается самим sentry_sdk (повторный init переинициализирует).
    """
    settings = settings or get_settings()
    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn is not None else ""
    if not dsn:
        logger.info("sentry_disabled_no_dsn")
        return False

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_effective_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        before_send=before_send,
        integrations=[StarletteIntegration(), CeleryIntegration()],
    )
    logger.info("sentry_initialized", extra={"environment": settings.sentry_effective_environment})
    return True


@contextmanager
def request_scope() -> Iterator[None]:
    """Изолированный Sentry-scope на время обработки запроса/таски (ADR-015 §4).

    Открывает `sentry_sdk.isolation_scope()`, чтобы correlation-теги, проставленные внутри
    (`set_correlation`), действовали на активный scope ДО возможного исключения и не протекали
    в соседние запросы под общим event-loop. Исключение, поднятое внутри `with`, остаётся в
    этом scope (с уже проставленными тегами) и пробрасывается дальше — Sentry-интеграция
    захватывает его с тегами. No-op (пустой контекст), если Sentry не установлен.
    """
    try:
        import sentry_sdk
    except ImportError:
        yield
        return
    with sentry_sdk.isolation_scope():
        yield


def set_correlation(
    *,
    job_id: str | None = None,
    project_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Проставляет correlation-теги job_id/project_id/user_id в активный Sentry-scope (§4).

    Единственное место, где высококардинальные идентификаторы попадают в observability
    (в Prometheus-labels запрещены). Должна вызываться ДО возможного исключения текущего
    запроса/таски (в api — auth-dependency внутри `request_scope`; в воркерах — обёртка таски
    в scope до тела), чтобы исключение несло теги. No-op, если Sentry не инициализирован.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    if job_id is not None:
        sentry_sdk.set_tag("job_id", job_id)
    if project_id is not None:
        sentry_sdk.set_tag("project_id", project_id)
    if user_id is not None:
        sentry_sdk.set_tag("user_id", user_id)


# Параметры Celery-тасок, несущие correlation-идентификаторы. Извлекаются по ИМЕНИ из
# args/kwargs таски (а не по позиции) — единообразно для всех тасок без правки каждой:
# pipeline.task_* несут job_id; deploy.rollback_revision — job_id+project_id; project.gc —
# project_id; notify.apns_push — job_id; beat-задачи аргументов не несут (теги не ставятся).
# instruction/промты/любые НЕ-идентификаторы СЮДА НЕ добавляются (не теги — §4 / 05-security).
_CORRELATION_PARAMS: tuple[str, ...] = ("job_id", "project_id", "user_id")


def correlation_from_task_args(
    param_names: Sequence[str],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> dict[str, str]:
    """Сопоставляет позиционные/именованные аргументы Celery-таски с correlation-тегами.

    `param_names` — имена параметров оборачиваемой функции таски (по порядку). Позиционные
    `args` сопоставляются с именами по позиции, затем перекрываются `kwargs`. Возвращает
    только job_id/project_id/user_id (см. `_CORRELATION_PARAMS`), приведённые к str и
    непустые — высококардинальные идентификаторы, единственные, что идут в Sentry-теги (§4).
    Промты/инструкции/секреты в выборку не попадают (нет в `_CORRELATION_PARAMS`).
    """
    bound: dict[str, Any] = {}
    for name, value in zip(param_names, args, strict=False):
        bound[name] = value
    bound.update(kwargs)
    result: dict[str, str] = {}
    for name in _CORRELATION_PARAMS:
        value = bound.get(name)
        if isinstance(value, str) and value:
            result[name] = value
    return result
