"""Триггер APNs push из обработчика переходов (Sprint 5, ADR-013, docs/notify §2).

Постановка notify.apns_push ПОСЛЕ коммита перехода (не в БД-транзакции — внешний
side-effect; потеря push при краше допустима, best-effort). Вызывается из единого
обработчика публикации перехода (app/pipeline/events.transition, тот же шаг, что
job_events + Redis publish). Ленивый импорт celery-таски — анти-цикл api↔workers
(как dispatch_for_state).

Нормативный перечень push-состояний — ADR-013 §3 (LIVE/FAILED/AWAITING_CLARIFICATION);
фильтр should_push живёт в notify.tasks (единственный источник перечня).
"""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger(__name__)


def enqueue_push_if_significant(job_id: str, to_state: str | None) -> None:
    """Ставит notify.apns_push, если to_state — значимое push-состояние (ADR-013 §3).

    No-op для промежуточных переходов и None. Постановка best-effort: ошибка enqueue
    (Redis-брокер недоступен) логируется, но НЕ ломает пайплайн (переход уже закоммичен).
    """
    if to_state is None:
        return
    from app.notify.tasks import apns_push, should_push

    if not should_push(to_state):
        return
    try:
        apns_push.apply_async(args=[job_id, to_state], queue="llm")
    except Exception as exc:  # noqa: BLE001 - best-effort: фейл enqueue не ломает пайплайн
        logger.warning(
            "apns_push_enqueue_failed",
            extra={"job_id": job_id, "to_state": to_state, "error": str(exc)},
        )
