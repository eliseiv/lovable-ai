"""Celery-приложение (docs/02-tech-stack.md, ADR-001/ADR-003).

Два namespace-очереди: queue=llm (агенты, масштаб по rate-limit Claude),
queue=build (сборка/деплой, масштаб по CPU). Брокер + result backend — Redis.
Запуск: celery -A app.workers.celery_app worker -Q <llm|build>.
"""

from __future__ import annotations

import inspect
from contextlib import ExitStack
from typing import Any

from celery import Celery
from celery.signals import task_postrun, task_prerun, worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.observability import sentry
from app.observability.exposition import start_worker_metrics_server

_settings = get_settings()
configure_logging(_settings.log_level)
# Sprint 6 (ADR-015): Sentry init для Celery (CeleryIntegration). Пустой SENTRY_DSN → no-op.
sentry.init_sentry(_settings)


@worker_process_init.connect
def _start_metrics_server(**_kwargs: object) -> None:
    """Поднимает HTTP-сервер метрик воркера/beat на METRICS_PORT (ADR-015 §1).

    Один сервер на worker-процесс (сигнал worker_process_init). Ошибка bind не валит воркер.
    """
    start_worker_metrics_server()


# Sprint 6 (ADR-015 §4, observability §4): Sentry-correlation для ВСЕХ Celery-тасок —
# единая точка через signal task_prerun/task_postrun (без дублирования в каждой таске).
# task_prerun открывает изолированный Sentry-scope (sentry.request_scope → isolation_scope)
# и проставляет теги job_id/project_id/user_id из аргументов таски ДО её тела — поэтому любое
# исключение в таске несёт correlation. task_postrun закрывает scope: теги одной таски не
# протекают в соседнюю (важно при reuse worker-процесса между тасками). No-op без Sentry
# (request_scope/set_correlation сами no-op, если sentry-sdk не сконфигурирован).
# Параллель api-корреляции: middleware request_scope + auth-dependency set_correlation(user_id)
# — здесь та же пара (scope + теги), но идентификаторы берутся из аргументов таски.
_correlation_scopes: dict[str, ExitStack] = {}


def _task_param_names(task: Any) -> list[str]:
    """Имена параметров оборачиваемой функции таски (для маппинга позиционных args по имени)."""
    try:
        return list(inspect.signature(task.run).parameters)
    except (TypeError, ValueError):  # pragma: no cover — у таски всегда есть .run
        return []


@task_prerun.connect
def _open_correlation_scope(
    *,
    task_id: str,
    task: Any = None,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    **_extra: object,
) -> None:
    """Открывает изолированный Sentry-scope таски + ставит correlation-теги ДО её тела (§4)."""
    stack = ExitStack()
    stack.enter_context(sentry.request_scope())
    if task is not None:
        tags = sentry.correlation_from_task_args(_task_param_names(task), args or (), kwargs or {})
        sentry.set_correlation(**tags)
    _correlation_scopes[task_id] = stack


@task_postrun.connect
def _close_correlation_scope(*, task_id: str, **_extra: object) -> None:
    """Закрывает Sentry-scope таски (теги не протекают в следующую таску процесса)."""
    stack = _correlation_scopes.pop(task_id, None)
    if stack is not None:
        stack.close()


celery_app = Celery(
    "lovable",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=[
        "app.workers.tasks",
        "app.workers.beat_tasks",
        # Sprint 3.5 billing beat-задачи (ресинк + grace-teardown sweep).
        "app.billing.resync",
        "app.billing.subscription_sweeper",
        # Sprint 4: project.gc — полный GC ресурсов проекта при удалении (ADR-011).
        "app.deploy.project_gc",
        # Sprint 5: notify.apns_push (APNs push статуса, ADR-013) + rollback ревизии (ADR-014).
        "app.notify.tasks",
        "app.deploy.rollback",
    ],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_default_queue="llm",
    timezone="UTC",
    enable_utc=True,
)

# Beat-расписание задаётся В КОДЕ (docs/07-deployment.md: beat-сервис поднят без
# CLI-расписания). Интервалы — из конфига (env-контракт Sprint 2, docs §E):
# sweeper уточнений (CLARIFICATION_SWEEP_INTERVAL_S) и reconciler застрявших
# джоб (RECONCILE_INTERVAL_S). Beat — единственный экземпляр.
celery_app.conf.beat_schedule = {
    "sweep-clarifications": {
        "task": "beat.sweep_clarifications",
        "schedule": float(_settings.clarification_sweep_interval_s),
    },
    "reconcile-stuck": {
        "task": "beat.reconcile_stuck",
        "schedule": float(_settings.reconcile_interval_s),
    },
    # Sprint 3.5: getProfile-ресинк (fallback на пропущенные вебхуки, ADR-009).
    "billing-resync": {
        "task": "billing.resync",
        "schedule": float(_settings.billing_resync_interval_s),
    },
    # Sprint 3.5: grace-teardown сайтов при истечении grace-периода (ADR-009 §C).
    "billing-subscription-sweep": {
        "task": "billing.subscription_sweep",
        "schedule": float(_settings.subscription_sweep_interval_s),
    },
    # Sprint 6 (ADR-015): refresh snapshot-gauge метрик (jobs_in_state/queue_depth/spend).
    # Интервал ≈ scrape-интервал Prometheus (PROMETHEUS_SCRAPE_INTERVAL_S) — снимок свежий.
    "metrics-refresh": {
        "task": "metrics.refresh",
        "schedule": float(_settings.prometheus_scrape_interval_s),
    },
}
