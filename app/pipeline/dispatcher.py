"""Диспетчер task-на-состояние (ADR-001, docs/modules/pipeline/03-architecture.md).

По текущему generation_jobs.state ставит следующий Celery-task в нужную очередь.
Пауза на AWAITING_CLARIFICATION — ноль задач в очереди. Резюм из POST /answers.

Импорт celery-тасок ленивый (внутри функции), чтобы избежать циклического импорта
api ↔ workers и держать API независимым от тяжёлых зависимостей при импорте.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.enums import JobState

logger = get_logger(__name__)


def dispatch_for_state(job_id: str, state: JobState, *, kind: str = "generation") -> None:
    """Ставит следующий task по состоянию. Безопасно для устойчивых состояний (no-op).

    kind различает стартовую таску в CREATED (Sprint 5, ADR-014): generation → task_interview
    (Agent 1 интервью), edit → task_edit (Agent 4 editor: спека + good-ревизия + instruction).
    Прочие состояния (SPECCING/BUILDING/DEPLOYING/FIXING) одинаковы для обоих kind — edit-цикл
    переиспускает ту же build/deploy/FIXING-машинерию (ADR-014 §A).
    """
    from app.workers import tasks

    if state == JobState.CREATED:
        if kind == "edit":
            tasks.task_edit.apply_async(args=[job_id], queue="llm")
        else:
            tasks.task_interview.apply_async(args=[job_id], queue="llm")
    elif state == JobState.SPECCING:
        tasks.task_spec.apply_async(args=[job_id], queue="llm")
    elif state == JobState.BUILDING:
        tasks.task_build_request.apply_async(args=[job_id], queue="build")
    elif state == JobState.DEPLOYING:
        tasks.task_deploy.apply_async(args=[job_id], queue="build")
    elif state == JobState.FIXING:
        # Sprint 2: восстановительный цикл — Agent 4 (queue=llm), гарды §C внутри таски.
        tasks.task_fix.apply_async(args=[job_id], queue="llm")
    else:
        # AWAITING_CLARIFICATION / LIVE / FAILED / INTERVIEWING — задач нет.
        logger.info("dispatch_noop", extra={"job_id": job_id, "state": state.value})
