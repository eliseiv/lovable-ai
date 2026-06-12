"""Router /jobs (docs/modules/api/02-api-contracts.md).

GET /jobs/{jid} (канонический статус), GET /jobs/{jid}/questions,
POST /jobs/{jid}/answers (детерминированная матрица 200/202/409/422).
Cross-tenant: джоба фильтруется по user_id владельца, 404 если не своя.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api import sse
from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import (
    conflict,
    not_found,
    problem_responses,
    too_many_requests,
    unprocessable,
)
from app.db.enums import JobState
from app.db.models import GenerationJob, Question
from app.schemas.api import (
    JobStatusResponse,
    QuestionOut,
    QuestionsResponse,
    SubmitAnswersRequest,
    SubmitAnswersResponse,
)
from app.services import project_service
from app.services.answers_service import AnswersOutcome, submit_answers

router = APIRouter(prefix="/jobs", tags=["Джобы генерации"])


async def _load_owned_job(session: SessionDep, user_id: str, job_id: str) -> GenerationJob:
    result = await session.execute(
        select(GenerationJob).where(GenerationJob.id == job_id, GenerationJob.user_id == user_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise not_found("Job not found.")
    return job


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Статус задачи",
    description=(
        "Возвращает текущий статус задачи генерации или правки. Поле `state` отражает этап "
        "(`CREATED`, `INTERVIEWING`, `AWAITING_CLARIFICATION`, `SPECCING`, `EDITING`, "
        "`BUILDING`, `DEPLOYING`, `LIVE`, `FIXING`, `FAILED`). `EDITING` — применение правки "
        "агентом-редактором. По завершении (`LIVE`) заполняется "
        "`live_url`; при неудаче (`FAILED`) — `failure_reason`. Чужая или несуществующая "
        "задача → `404`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def get_job(job_id: str, user: CurrentUser, session: SessionDep) -> JobStatusResponse:
    job = await _load_owned_job(session, user.id, job_id)
    live_url = None
    if job.state == JobState.LIVE:
        live_url = await project_service.get_project_live_url(session, job.project_id)
    return JobStatusResponse(
        id=job.id,
        project_id=job.project_id,
        state=job.state.value,
        retry_count=job.retry_count,
        failure_reason=job.failure_reason,
        live_url=live_url,
        updated_at=job.updated_at,
    )


@router.get(
    "/{job_id}/questions",
    response_model=QuestionsResponse,
    summary="Уточняющие вопросы",
    description=(
        "Возвращает список уточняющих вопросов, которые сервис задаёт для уточнения задачи. "
        "Доступно, когда задача ожидает ответов пользователя (`state` = "
        "`AWAITING_CLARIFICATION`). Ответы отправляются методом "
        "`POST /jobs/{jid}/answers`. Чужая или несуществующая задача → `404`. "
        "Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def get_questions(job_id: str, user: CurrentUser, session: SessionDep) -> QuestionsResponse:
    await _load_owned_job(session, user.id, job_id)
    result = await session.execute(
        select(Question).where(Question.job_id == job_id).order_by(Question.position)
    )
    questions = [QuestionOut.model_validate(q) for q in result.scalars().all()]
    return QuestionsResponse(questions=questions)


@router.post(
    "/{job_id}/answers",
    response_model=SubmitAnswersResponse,
    summary="Отправить ответы на уточняющие вопросы",
    description=(
        "Отправляет ответы на уточняющие вопросы и продолжает генерацию. Нужно ответить на "
        "все обязательные вопросы задачи. Успешная отправка возвращает `202`. Повторная "
        "отправка тех же ответов идемпотентна и возвращает `200`. Если задача уже "
        "продолжена с другими ответами или завершена — `409`. Неполный или некорректный "
        "набор ответов → `422`. Чужая или несуществующая задача → `404`. Требуется "
        "заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 409, 422, 429),
)
async def post_answers(
    job_id: str,
    body: SubmitAnswersRequest,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
) -> SubmitAnswersResponse:
    job = await _load_owned_job(session, user.id, job_id)
    result = await submit_answers(session, job=job, items=body.answers)

    if result.outcome == AnswersOutcome.APPLIED:
        response.status_code = status.HTTP_202_ACCEPTED
        return SubmitAnswersResponse(job_id=result.job_id)
    if result.outcome == AnswersOutcome.IDEMPOTENT:
        response.status_code = status.HTTP_200_OK
        return SubmitAnswersResponse(job_id=result.job_id)
    if result.outcome == AnswersOutcome.CONFLICT:
        raise conflict(result.detail or "Conflict.", current_state=result.current_state)
    raise unprocessable(result.detail or "Invalid answers payload.")


def _parse_last_event_id(header_value: str | None, query_value: int | None) -> int | None:
    """Last-Event-ID: заголовок (приоритетнее) или query ?last_event_id. Невалидный → None.

    None = подключение без reconnect-точки → первый кадр = текущий снимок (ADR-012 §2).
    Невалидный заголовок не должен ронять стрим (трактуем как отсутствие).
    """
    if header_value is not None:
        try:
            return int(header_value)
        except ValueError:
            return None
    return query_value


@router.get(
    "/{job_id}/events",
    summary="Поток событий задачи (SSE)",
    description=(
        "Поток событий задачи в реальном времени в формате Server-Sent Events "
        "(`text/event-stream`). Каждое событие несёт идентификатор `id`; при "
        "переподключении клиент передаёт заголовок `Last-Event-ID` (или query-параметр "
        "`last_event_id`), чтобы получить пропущенные события. Поток завершается событием "
        "`done` при достижении конечного статуса (`LIVE` или `FAILED`).\n\n"
        "Количество одновременных потоков на ключ ограничено — при превышении `429`. Чужая "
        "или несуществующая задача → `404`. Альтернатива потоку — опрос статуса "
        "`GET /jobs/{jid}`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def job_events(
    job_id: str,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id: int | None = None,
) -> StreamingResponse:
    """Поток событий задачи (SSE) с переподключением по Last-Event-ID и heartbeat.

    Завершение событием `done` на конечном статусе; лимит потоков на ключ → 429; чужая
    задача → 404. Опрос `GET /jobs/{jid}` — равноправная альтернатива.
    """
    await _load_owned_job(session, user.id, job_id)
    resume_from = _parse_last_event_id(last_event_id_header, last_event_id)

    # Лимит одновременных стримов на ключ (ADR-012 §7). key_id текущего запроса проставлен
    # dependency get_current_user (новый формат ключа). Legacy-ключ без key_id → лимит по
    # user.id (тот же эффект — потолок на источник). Превышение → 429.
    stream_key = getattr(request.state, "current_token_key_id", None) or user.id
    slot = await sse.acquire_stream_slot(stream_key)
    if not slot.acquired:
        raise too_many_requests(
            "Too many concurrent SSE streams for this key.",
        )

    async def guarded_stream() -> AsyncIterator[bytes]:
        try:
            async for frame in sse.event_stream(job_id, last_event_id=resume_from):
                yield frame
        finally:
            # Освобождаем слот при закрытии стрима (нормальное завершение / отмена клиентом).
            await sse.release_stream_slot(stream_key)

    return StreamingResponse(guarded_stream(), media_type="text/event-stream")
