"""Celery-задача notify.apns_push (Sprint 5, ADR-013, docs/notify §2-3).

Триггер — обработчик переходов job_events (после коммита перехода) при to_state ∈
{LIVE, FAILED, AWAITING_CLARIFICATION} (нормативный перечень — ADR-013 §3). queue=llm
(лёгкая I/O-задача). Best-effort: потеря push не ломает джобу.

Поток (docs/notify §3):
  1. APNs не сконфигурирован → no-op (log skip), пайплайн цел.
  2. Владелец джобы → активные device_tokens (cross-tenant: только владелец).
  3. provider-JWT ES256 (кэш) + HTTP/2 POST /3/device на каждое устройство.
  4. 200 → last_push_at; 410/400 BadDeviceToken → invalidated_at; 429/5xx → Celery retry.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob
from app.db.session import session_scope
from app.notify import device_service
from app.notify.apns_client import (
    ApnsClient,
    ApnsConfigError,
    ApnsTransientError,
    build_payload,
)
from app.observability import metrics
from app.services import project_service
from app.workers.celery_app import celery_app
from app.workers.retry_policy import MAX_RETRIES, RETRY_BACKOFF_MAX_S

logger = get_logger(__name__)

# Нормативный перечень push-состояний (ADR-013 §3) — единственный источник.
_PUSH_STATES: frozenset[str] = frozenset(
    {JobState.LIVE.value, JobState.FAILED.value, JobState.AWAITING_CLARIFICATION.value}
)


def should_push(to_state: str) -> bool:
    """True, если переход в to_state генерирует push (ADR-013 §3). Промежуточные — нет."""
    return to_state in _PUSH_STATES


async def _resolve_live_url(session: AsyncSession, job: GenerationJob) -> str | None:
    """live_url активного деплоя для LIVE-push (deep-link). Иначе None."""
    if job.state != JobState.LIVE:
        return None
    return await project_service.get_project_live_url(session, job.project_id)


async def _apns_push(job_id: str, to_state: str) -> None:
    settings = get_settings()
    # 1. APNs не сконфигурирован → no-op (без credentials фича неактивна, пайплайн цел).
    if not settings.apns_configured:
        logger.info("apns_push_skip_unconfigured", extra={"job_id": job_id, "to_state": to_state})
        metrics.apns_push_total.labels(result="noop_no_credentials", apns_status="none").inc()
        return
    if not should_push(to_state):
        logger.info("apns_push_skip_state", extra={"job_id": job_id, "to_state": to_state})
        return

    async with session_scope() as session:
        job = await session.get(GenerationJob, job_id)
        if job is None:
            logger.info("apns_push_skip_no_job", extra={"job_id": job_id})
            return
        # 2. Cross-tenant: устройства строго владельца джобы.
        devices = await device_service.active_devices_for_user(session, job.user_id)
        if not devices:
            logger.info("apns_push_no_devices", extra={"job_id": job_id, "user_id": job.user_id})
            return
        live_url = await _resolve_live_url(session, job)
        payload = build_payload(to_state, job_id, live_url)

        client = ApnsClient(settings)
        delivered = 0
        try:
            for device in devices:
                # 3-4. Отправка; ApnsTransientError всплывает наверх → Celery retry (ниже).
                result = await client.send(
                    apns_token=device.apns_token,
                    device_environment=device.environment,
                    payload=payload,
                )
                if result.ok:
                    await device_service.mark_pushed(session, device.id)
                    delivered += 1
                elif result.invalid_token:
                    await device_service.mark_invalidated(session, device.id)
        except ApnsConfigError:
            # Credentials исчезли между apns_configured-чеком и отправкой (rotation/unmount)
            # → no-op (не падаем, пайплайн best-effort, ADR-013 §5).
            logger.info("apns_push_skip_config_lost", extra={"job_id": job_id})
            return
        await session.commit()
        logger.info(
            "apns_push_sent",
            extra={
                "job_id": job_id,
                "to_state": to_state,
                "devices": len(devices),
                "delivered": delivered,
            },
        )


@celery_app.task(
    name="notify.apns_push",
    queue="llm",
    autoretry_for=(ApnsTransientError,),
    retry_backoff=True,
    retry_backoff_max=RETRY_BACKOFF_MAX_S,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
)
def apns_push(job_id: str, to_state: str) -> None:
    """notify.apns_push — best-effort push на устройства владельца джобы (ADR-013).

    429/5xx APNs → ApnsTransientError → Celery autoretry с backoff (классификация как
    инфра-сбой, ADR-006). Исчерпание retries → best-effort drop (не блокирует пайплайн —
    Celery поглощает финальный фейл, переход джобы уже закоммичен).
    """
    asyncio.run(_apns_push(job_id, to_state))
