"""Grace-teardown сайтов через Celery-beat (docs/billing/03 §6, ADR-009 §C).

billing.subscription_sweep (SUBSCRIPTION_SWEEP_INTERVAL_S): выбирает subscriptions со
status='grace' AND grace_until < now() → teardown всех active-сайтов пользователя
(переиспользует deploy.teardown_container — docker rm -f + освобождение Traefik-route,
идемпотентно) → subscriptions.status=expired.

Renew в grace отменяет teardown: вебхук переводит status→active, grace_until=NULL → sweep
не выбирает. Гонка renew↔sweep — SELECT ... FOR UPDATE строки subscriptions: если к
моменту захвата строка уже active, sweep её пропускает. Гасятся ТОЛЬКО реально
задеплоенные active-сайты; building/failed/superseded не трогаются.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import subscription_state
from app.core.logging import get_logger
from app.db.models import Project, SiteDeployment, Subscription
from app.db.session import session_scope, worker_engine_scope
from app.deploy import docker_deploy
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _teardown_user_sites(session: AsyncSession, user_id: str) -> int:
    """Снос всех active-сайтов пользователя. Возвращает число снесённых деплоев.

    Идемпотентно: docker rm -f отсутствующего контейнера — не ошибка. status деплоя
    → superseded (ресурс снят без фейла деплоя), строка БД сохранена для аудита.
    """
    result = await session.execute(
        select(SiteDeployment)
        .join(Project, Project.id == SiteDeployment.project_id)
        .where(Project.user_id == user_id, SiteDeployment.status == "active")
    )
    deployments = list(result.scalars().all())
    for deployment in deployments:
        container_name = f"site_{deployment.subdomain}"
        # Синхронный docker rm -f в thread, чтобы не блокировать event-loop.
        await asyncio.to_thread(docker_deploy.teardown_container, container_name)
        deployment.status = "superseded"
        logger.info(
            "billing_grace_teardown",
            extra={"user_id": user_id, "subdomain": deployment.subdomain},
        )
    return len(deployments)


async def _sweep_subscriptions() -> int:
    """Grace-sweep: status='grace' AND grace_until < now() → teardown → status=expired."""
    now = datetime.now(UTC)
    swept = 0
    async with session_scope() as session:
        # Кандидаты под FOR UPDATE: блокируем строки subscriptions против гонки с renew
        # (вебхук subscription_renewed захватывает ту же строку). Если к моменту захвата
        # строка уже active (renew успел) — предикат в выборке её отфильтрует.
        result = await session.execute(
            select(Subscription)
            .where(
                Subscription.status == subscription_state.STATUS_GRACE,
                Subscription.grace_until.is_not(None),
                Subscription.grace_until < now,
            )
            .with_for_update(skip_locked=True)
        )
        subs = list(result.scalars().all())
        for sub in subs:
            # Re-check под блокировкой: renew мог перевести в active между выборкой
            # кандидатов и захватом (predicate в SELECT уже это учёл, но защищаемся явно).
            if sub.status != subscription_state.STATUS_GRACE:
                continue
            await _teardown_user_sites(session, sub.user_id)
            # Grace отработан → expired. Идемпотентно: повторный sweep уже-expired —
            # no-op (нет grace-строк / нет active-деплоев).
            sub.status = subscription_state.STATUS_EXPIRED
            sub.grace_until = None
            swept += 1
        await session.commit()
    if swept:
        logger.info("billing_subscriptions_swept", extra={"count": swept})
    return swept


@celery_app.task(name="billing.subscription_sweep", queue="build")
def subscription_sweep() -> int:
    """Celery-beat: grace-teardown сайтов. queue=build (docker-операции на build-воркере)."""

    async def _run() -> int:
        # observability §7 (ADR-019): per-task async-engine внутри asyncio.run-loop задачи.
        async with worker_engine_scope():
            return await _sweep_subscriptions()

    return asyncio.run(_run())
