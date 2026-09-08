"""Integration: повтор упавшей генерации `POST /jobs/{jid}/retry` (ADR-053).

Проверяется то, ради чего ручка сделана: проект и всё, что на нём висит (промпт, фото),
остаются прежними, ответы интервью переезжают в новую джобу, повтор не стоит генерации,
а число бесплатных повторов ограничено. Отказы: не-`FAILED`, чужая джоба, исчерпанный лимит.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.ids import new_answer_id, new_job_id, new_project_id, new_question_id
from app.db.enums import JobState
from app.db.models import (
    Answer,
    Attachment,
    GenerationJob,
    JobEvent,
    Project,
    Question,
    UsageCounter,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):  # noqa: ANN001, ANN201
    """Ретрай ставит Celery-таску — фиксируем факт постановки, саму таску не выполняем."""
    import app.services.job_retry_service as mod

    calls: list[tuple[str, JobState]] = []
    monkeypatch.setattr(
        mod, "dispatch_for_state", lambda job_id, state, **kw: calls.append((job_id, state))
    )
    return calls


async def _failed_job(
    session,  # noqa: ANN001
    user_id: str,
    *,
    answered: bool = True,
    questions: int = 2,
    state: JobState = JobState.FAILED,
    kind: str = "generation",
) -> GenerationJob:
    """Упавшая генерация с интервью: столько-то вопросов, при `answered` — с ответами."""
    project = Project(id=new_project_id(), user_id=user_id, prompt="лендинг кофейни", title=None)
    session.add(project)
    job = GenerationJob(
        id=new_job_id(),
        project_id=project.id,
        user_id=user_id,
        state=state,
        kind=kind,
        budget_usd=Decimal("5.0000"),
    )
    session.add(job)
    await session.flush()

    for position in range(questions):
        question = Question(
            id=new_question_id(),
            job_id=job.id,
            position=position,
            text=f"Вопрос {position}",
            kind="free_text",
            options=None,
        )
        session.add(question)
        if answered:
            session.add(
                Answer(
                    id=new_answer_id(),
                    question_id=question.id,
                    job_id=job.id,
                    text=f"Ответ {position}",
                )
            )
    await session.flush()
    return job


async def _retry(client, job_id: str, auth_headers):  # noqa: ANN001, ANN202
    return await client.post(f"/v1/jobs/{job_id}/retry", headers=auth_headers)


async def test_retry_keeps_project_and_returns_new_job(
    client, session, seeded_user, auth_headers, dispatched
):
    """Ради этого ручка и просилась: новый job_id, тот же project_id."""
    job = await _failed_job(session, seeded_user.id)

    resp = await _retry(client, job.id, auth_headers)

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] != job.id
    assert body["project_id"] == job.project_id
    assert body["retry_of_job_id"] == job.id
    assert body["charged"] is False
    # Таска поставлена — повтор реально пошёл в работу, а не просто создал строку.
    assert [jid for jid, _ in dispatched] == [body["job_id"]]


async def test_retry_copies_answers_and_skips_interview(client, session, seeded_user, auth_headers):
    """Ответы были полные — переспрашивать нечего, повтор стартует со спеки."""
    job = await _failed_job(session, seeded_user.id, answered=True, questions=2)

    resp = await _retry(client, job.id, auth_headers)

    assert resp.status_code == 202
    assert resp.json()["state"] == JobState.SPECCING.value
    retry_id = resp.json()["job_id"]
    copied_questions = (
        await session.execute(
            select(func.count()).select_from(Question).where(Question.job_id == retry_id)
        )
    ).scalar_one()
    copied_answers = (
        (
            await session.execute(
                select(Answer).where(Answer.job_id == retry_id).order_by(Answer.text)
            )
        )
        .scalars()
        .all()
    )
    assert copied_questions == 2
    assert [a.text for a in copied_answers] == ["Ответ 0", "Ответ 1"]
    # Копии, а не переезд: история упавшей попытки осталась при ней.
    original_answers = (
        await session.execute(
            select(func.count()).select_from(Answer).where(Answer.job_id == job.id)
        )
    ).scalar_one()
    assert original_answers == 2


async def test_retry_without_full_answers_runs_interview_again(
    client, session, seeded_user, auth_headers
):
    """Интервью не было доведено до конца — повтор начинается с него, а не со спеки."""
    job = await _failed_job(session, seeded_user.id, answered=False)

    resp = await _retry(client, job.id, auth_headers)

    assert resp.status_code == 202
    assert resp.json()["state"] == JobState.CREATED.value


async def test_retry_is_free(client, session, seeded_user, auth_headers):
    """Упавшая попытка уже списала генерацию — второй раз за наш сбой не берём."""
    job = await _failed_job(session, seeded_user.id)

    resp = await _retry(client, job.id, auth_headers)
    retry_id = resp.json()["job_id"]

    used = (
        await session.execute(
            select(func.count())
            .select_from(UsageCounter)
            .where(UsageCounter.user_id == seeded_user.id)
        )
    ).scalar_one()
    assert used == 0
    # Маркер списания проставлен заранее — фаза интервью увидит его и не спишет.
    markers = (
        (
            await session.execute(
                select(JobEvent).where(
                    JobEvent.job_id == retry_id, JobEvent.event_type == "usage_counted"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(markers) == 1
    assert markers[0].payload["source"] == "retry"


async def test_retry_keeps_attachments_of_the_project(client, session, seeded_user, auth_headers):
    """Фото висят на проекте — повтор в том же проекте видит их без перезаливки."""
    job = await _failed_job(session, seeded_user.id)
    session.add(
        Attachment(
            id="att_retry000000001",
            project_id=job.project_id,
            job_id=job.id,
            s3_ref=f"uploads/{job.project_id}/att_retry000000001.jpg",
            filename="photo.jpg",
            mime="image/jpeg",
            size_bytes=1024,
            width=100,
            height=100,
        )
    )
    await session.flush()

    resp = await _retry(client, job.id, auth_headers)

    assert resp.json()["project_id"] == job.project_id
    attachments = (
        await session.execute(
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.project_id == job.project_id)
        )
    ).scalar_one()
    assert attachments == 1


async def test_repeated_retry_returns_the_same_job(client, session, seeded_user, auth_headers):
    """Повторный вызов не плодит попытки: клиент мог не получить первый ответ."""
    job = await _failed_job(session, seeded_user.id)

    first = await _retry(client, job.id, auth_headers)
    second = await _retry(client, job.id, auth_headers)

    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["job_id"] == second.json()["job_id"]
    total = (
        await session.execute(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.project_id == job.project_id)
        )
    ).scalar_one()
    assert total == 2


async def test_retry_limit_is_enforced(client, session, seeded_user, auth_headers, settings):
    """Бесплатные повторы конечны — иначе одна оплаченная генерация крутилась бы вечно."""
    job = await _failed_job(session, seeded_user.id)

    current = job
    for _ in range(settings.generation_retry_max_attempts):
        resp = await _retry(client, current.id, auth_headers)
        assert resp.status_code == 202
        current = await session.get(GenerationJob, resp.json()["job_id"])
        assert current is not None
        current.state = JobState.FAILED
        await session.flush()

    exhausted = await _retry(client, current.id, auth_headers)
    assert exhausted.status_code == 409


@pytest.mark.parametrize("state", [JobState.LIVE, JobState.BUILDING, JobState.CREATED])
async def test_retry_of_not_failed_job_is_409(client, session, seeded_user, auth_headers, state):
    """Повторяют упавшее. Живой сайт или идущая генерация — не повод стартовать вторую."""
    job = await _failed_job(session, seeded_user.id, state=state)

    resp = await _retry(client, job.id, auth_headers)

    assert resp.status_code == 409


async def test_retry_of_edit_job_is_409(client, session, seeded_user, auth_headers):
    """Правка — не генерация: у неё свой счётчик и свой путь перезапуска."""
    job = await _failed_job(session, seeded_user.id, kind="edit")

    resp = await _retry(client, job.id, auth_headers)

    assert resp.status_code == 409


async def test_retry_of_foreign_job_is_404(client, session, seeded_user, other_user, auth_headers):
    """Чужая джоба неотличима от несуществующей."""
    job = await _failed_job(session, other_user.id)

    resp = await _retry(client, job.id, auth_headers)

    assert resp.status_code == 404


async def test_retry_requires_auth(client, session, seeded_user):
    job = await _failed_job(session, seeded_user.id)

    resp = await client.post(f"/v1/jobs/{job.id}/retry")

    assert resp.status_code == 401
