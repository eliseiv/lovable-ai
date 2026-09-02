"""План сайта и посекционный прогресс генерации (ADR-046).

Agent 2 отдаёт вместе со спекой список секций, которые будет строить Agent 3; строки
`job_sections` фиксируют этот план и его выполнение. Секция помечается готовой, когда её
идентификатор встречается в потоке кодогенерации Agent 3 — прогресс идёт по мере вывода
модели, а не появляется разом в конце шага.

Прогресс — вспомогательный UX-канал: любые сбои детекта/публикации не должны влиять на
генерацию, поэтому хук глушит свои ошибки (логируя их), а источник истины по результату
шага остаётся прежним (`generation_jobs.state` + `job_events`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import JobSection
from app.db.session import session_scope
from app.pipeline.agents.agent2 import PlannedSection
from app.pipeline.agents.base import TextDeltaHook
from app.pipeline.events import publish_event, record_event

logger = get_logger(__name__)

SECTION_STATUS_PENDING = "pending"
SECTION_STATUS_DONE = "done"


async def save_plan(
    session: AsyncSession, job_id: str, sections: Sequence[PlannedSection]
) -> list[JobSection]:
    """Сохраняет план джобы (`pending`). Пустой план → пустой список, строк нет.

    Коммит — на стороне вызывающего (план пишется в той же транзакции, что и спека).
    """
    rows = [
        JobSection(
            job_id=job_id,
            section_id=section.id,
            title=section.title,
            position=position,
            status=SECTION_STATUS_PENDING,
        )
        for position, section in enumerate(sections)
    ]
    session.add_all(rows)
    return rows


async def list_sections(session: AsyncSession, job_id: str) -> list[JobSection]:
    """План джобы по порядку следования секций (пусто, если плана нет)."""
    result = await session.execute(
        select(JobSection).where(JobSection.job_id == job_id).order_by(JobSection.position)
    )
    return list(result.scalars().all())


async def _mark_done(job_id: str, section_id: str, title: str) -> None:
    """Отмечает секцию готовой в своей короткой транзакции + доставляет событие клиенту.

    Своя сессия, а не сессия шага: шаг держит открытую транзакцию всего шага генерации,
    и коммит прогресса в ней опубликовал бы незавершённое состояние шага.

    Событие пишется в `job_events` И публикуется в Redis — ровно как переходы state
    (ADR-012 §2): SSE-кадры формируются ИЗ `job_events` (там же replay по Last-Event-ID),
    а pub/sub — лишь wake-сигнал. Без записи в БД событие не дошло бы до клиента вовсе.
    """
    payload = {"section_id": section_id, "title": title}
    async with session_scope() as progress_session:
        await progress_session.execute(
            update(JobSection)
            .where(
                JobSection.job_id == job_id,
                JobSection.section_id == section_id,
                JobSection.status != SECTION_STATUS_DONE,
            )
            .values(status=SECTION_STATUS_DONE, completed_at=datetime.now(UTC))
        )
        await record_event(progress_session, job_id, "section_completed", payload=payload)
        await progress_session.commit()
    await publish_event(job_id, "section_completed", payload=payload)


def make_progress_hook(job_id: str, sections: Sequence[PlannedSection]) -> TextDeltaHook | None:
    """Хук потока Agent 3, отмечающий секции по мере их появления в выводе (ADR-046 §B).

    Возвращает `None` при пустом плане — тогда стрим не итерируется вовсе и путь
    кодогенерации остаётся прежним.

    Детект — вхождение `section_id` в накопленном тексте (регистронезависимо). Это
    эвристика: она отвечает на вопрос «модель уже дошла до этой секции», а не даёт
    криптографической гарантии, что секция полностью записана. Цена ошибки — неточная
    галочка в чате, поэтому эвристика предпочтена переработке пайплайна на посекционную
    генерацию (дороже по токенам и времени).

    Буфер — только хвост длиной с самый длинный идентификатор: он покрывает разрыв токена
    между чанками, а память не растёт с длиной ответа.
    """
    if not sections:
        return None

    pending = {section.id: section.title for section in sections}
    overlap = max(len(section.id) for section in sections)
    carry = ""

    async def on_text_delta(chunk: str) -> None:
        nonlocal carry
        if not pending:
            return
        window = (carry + chunk).lower()
        try:
            for section_id in [sid for sid in pending if sid in window]:
                title = pending.pop(section_id)
                await _mark_done(job_id, section_id, title)
        except Exception as exc:  # noqa: BLE001 — прогресс не смеет ронять генерацию
            logger.warning(
                "section_progress_failed",
                extra={"job_id": job_id, "error": str(exc)},
            )
        finally:
            carry = window[-overlap:] if overlap else ""

    return on_text_delta


__all__ = [
    "SECTION_STATUS_DONE",
    "SECTION_STATUS_PENDING",
    "list_sections",
    "make_progress_hook",
    "save_plan",
]
