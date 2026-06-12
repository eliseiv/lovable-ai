"""ADR-029 — инвариант терминальности состояния: CAS-барьер transition() (барьер A).

Нормативный источник — docs/adr/ADR-029-terminal-state-invariant-no-overwrite-deploy-guard-
reconciler-revoke.md §Decision A, docs/06-testing-strategy.md §Integration «Инвариант
терминальности — race FAILED↔LIVE», docs/modules/pipeline/03-architecture.md §Инвариант
терминальности состояния; app/pipeline/events.py (transition/fail_job/touch_heartbeat).

Прод-фикс рассинхрона FAILED↔LIVE: transition() стал conditional UPDATE (CAS) —
`UPDATE ... SET state=:to WHERE id=:id AND state NOT IN (LIVE,FAILED)`. 0 строк ⇒ джоба уже
терминальна ⇒ переход — no-op (возврат False, job_events НЕ пишется, publish/push/terminal-
метрики НЕ дублируются, ORM staged-write откатывается через session.rollback()). 1 строка ⇒
переход применён (возврат True).

КРИТИЧНО про изоляцию: no-op путь transition() делает `await session.rollback()`. Поэтому тесты
no-op используют `autonomous_db` + `session_scope` (отдельные реальные транзакции, как
beat/deploy-тесты): джоба коммитится в предыдущем session_scope-блоке и переживает rollback
внутри transition(). In-transaction conftest-фикстура `session` (SAVEPOINT) для no-op НЕ годится —
rollback внутри transition схлопнул бы savepoint и удалил бы flush'нутую джобу. Happy-path /
heartbeat (без rollback) — на простой `session`-фикстуре.

Покрывает нормативный чек-лист (docs §47-51):
- CAS no-op: FAILED → transition(LIVE) → False, state остаётся FAILED, нового job_events нет;
- симметрично: LIVE → transition(FAILED) → False, state остаётся LIVE;
- happy-path: DEPLOYING → transition(LIVE) → True, state=LIVE, job_events создан;
- все легитимные переходы из НЕ-терминальных состояний применяются (True);
- bool-контракт fail_job: повторный FAILED на уже-FAILED → no-op, метрики не дублируются;
- fail_job на не-терминальной → применён, метрика инкрементирована один раз;
- touch_heartbeat двигает last_transition_at без смены state и без job_events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User
from app.db.session import session_scope
from app.observability import metrics
from app.pipeline.events import fail_job, touch_heartbeat, transition

pytestmark = pytest.mark.asyncio

UID = "u_adr029owner0000000a"


# ===========================================================================
# Хелперы / фикстуры для no-op тестов (autonomous_db + session_scope).
# ===========================================================================


@pytest.fixture(autouse=True)
def _neutralize_side_effects(monkeypatch):  # noqa: ANN001, ANN202
    """publish_event (Redis) + APNs push — внешние границы; spy для проверки no-op не дёргает их."""
    publishes: list = []
    pushes: list = []

    async def _spy_publish(job_id, event_type, **kwargs):  # noqa: ANN001, ANN202
        publishes.append((job_id, event_type, kwargs))

    def _spy_push(job_id, to_state):  # noqa: ANN001, ANN202
        pushes.append((job_id, to_state))

    import app.pipeline.events as events

    monkeypatch.setattr(events, "publish_event", _spy_publish)
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", _spy_push)
    return {"publishes": publishes, "pushes": pushes}


async def _purge() -> None:
    async with session_scope() as s:
        jids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == UID)
                    )
                )
                .scalars()
                .all()
            )
        )
        if jids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(jids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest_asyncio.fixture
async def auto_env(autonomous_db):  # noqa: ANN001, ANN201
    await _purge()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("adr029-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        await s.commit()
    yield
    await _purge()


async def _make_committed_job(state: JobState) -> str:
    """Создаёт+коммитит джобу (отдельная транзакция) — переживает rollback внутри transition()."""
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(Project(id=pid, user_id=UID, prompt="p", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=state,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
            )
        )
        await s.commit()
    return jid


async def _state_of(jid: str) -> JobState:
    async with session_scope() as s:
        return await s.scalar(select(GenerationJob.state).where(GenerationJob.id == jid))


async def _event_count(jid: str, *, event_type: str | None = None) -> int:
    async with session_scope() as s:
        q = select(func.count()).select_from(JobEvent).where(JobEvent.job_id == jid)
        if event_type is not None:
            q = q.where(JobEvent.event_type == event_type)
        return await s.scalar(q)


def _counter_value(counter, **labels) -> float:  # noqa: ANN001
    """Текущее значение Prometheus Counter по labels (для проверки отсутствия дубля)."""
    return counter.labels(**labels)._value.get()


# ===========================================================================
# 1. CAS no-op: терминал НЕ перезатирается (барьер A — корень).
# ===========================================================================


async def test_transition_to_live_on_failed_is_noop(auto_env, _neutralize_side_effects):
    """FAILED → transition(LIVE) → False (no-op): state остаётся FAILED, нового job_events нет.

    Инцидентный путь прод-бага: живая task_deploy добежала и пишет LIVE поверх уже-FAILED
    (записанного reconciler'ом). CAS-барьер обязан вернуть False и НЕ перезаписать state.
    """
    jid = await _make_committed_job(JobState.FAILED)
    before_events = await _event_count(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        applied = await transition(s, job, JobState.LIVE, event_type="state_changed")

    assert applied is False  # no-op (0 строк CAS)
    assert await _state_of(jid) == JobState.FAILED  # FAILED НЕ перезаписан на LIVE
    assert await _event_count(jid) == before_events  # нового job_events нет
    assert _neutralize_side_effects["publishes"] == []  # publish НЕ вызван
    assert _neutralize_side_effects["pushes"] == []  # push НЕ вызван


async def test_transition_to_failed_on_live_is_noop(auto_env, _neutralize_side_effects):
    """Симметрично: LIVE → transition(FAILED) → False (no-op), state остаётся LIVE.

    Терминал, записанный первым (LIVE), побеждает: проигравший писатель не перезаписывает LIVE.
    """
    jid = await _make_committed_job(JobState.LIVE)
    before_events = await _event_count(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        applied = await transition(s, job, JobState.FAILED, event_type="failed")

    assert applied is False
    assert await _state_of(jid) == JobState.LIVE  # LIVE НЕ перезаписан на FAILED
    assert await _event_count(jid) == before_events
    assert _neutralize_side_effects["publishes"] == []


async def test_fail_job_on_already_failed_does_not_double_count_metrics(
    auto_env, _neutralize_side_effects
):
    """fail_job на уже-FAILED → transition no-op → job_failed_total/jobs_total НЕ дублируются.

    ADR-029 §A / fail_job: job_failed_total инкрементится ТОЛЬКО если CAS реально применил переход.
    Проигравший писатель (reconciler), пытающийся FAILED поверх уже-FAILED, НЕ должен ложно
    инкрементировать failure-метрику на no-op (иначе двойной учёт терминала).
    """
    jid = await _make_committed_job(JobState.FAILED)

    failed_before = _counter_value(
        metrics.job_failed_total, reason="stuck_timeout", kind="generation"
    )
    jobs_before = _counter_value(metrics.jobs_total, kind="generation", terminal_state="FAILED")

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await fail_job(s, job, failure_reason="stuck_timeout")

    failed_after = _counter_value(
        metrics.job_failed_total, reason="stuck_timeout", kind="generation"
    )
    jobs_after = _counter_value(metrics.jobs_total, kind="generation", terminal_state="FAILED")

    assert failed_after == failed_before  # no-op → job_failed_total НЕ инкрементирован
    assert jobs_after == jobs_before  # no-op → jobs_total НЕ инкрементирован
    assert await _state_of(jid) == JobState.FAILED


async def test_failed_then_live_writes_single_terminal_event_and_metric(
    auto_env, _neutralize_side_effects
):
    """Гонка: FAILED записан первым, затем добежавший LIVE → итог один терминал, без дубля метрик.

    jobs_total{terminal_state=FAILED} +1 на первом FAILED; повторный LIVE — no-op (нет +1 на
    terminal_state=LIVE). Ровно одно терминальное job_events. Доказывает отсутствие дубля
    терминал-метрик/SSE done на проигравшем писателе (ADR-029 §Consequences «Идемпотентность»).
    """
    jid = await _make_committed_job(JobState.DEPLOYING)
    live_before = _counter_value(metrics.jobs_total, kind="generation", terminal_state="LIVE")

    # 1) reconciler/fail_job записал FAILED первым (применён).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert await fail_job(s, job, failure_reason="wall_clock_exceeded") is None

    # 2) добежавшая task_deploy пытается LIVE поверх FAILED → no-op.
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        applied_live = await transition(s, job, JobState.LIVE, event_type="state_changed")

    assert applied_live is False
    live_after = _counter_value(metrics.jobs_total, kind="generation", terminal_state="LIVE")
    assert live_after == live_before  # terminal_state=LIVE НЕ инкрементирован (LIVE не применён)
    assert await _state_of(jid) == JobState.FAILED
    assert await _event_count(jid, event_type="failed") == 1  # ровно одно терминальное событие
    assert await _event_count(jid, event_type="state_changed") == 0  # нет state_changed→LIVE


# ===========================================================================
# 2. CAS happy-path + heartbeat: на in-transaction `session` (без rollback внутри).
# ===========================================================================


async def _make_job_in_session(session, user_id, state):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title=None))
    job = GenerationJob(
        id=jid,
        project_id=pid,
        user_id=user_id,
        state=state,
        kind="generation",
        budget_usd=Decimal("5.0000"),
        spend_usd=Decimal("0.0000"),
    )
    session.add(job)
    await session.flush()
    return job


async def test_transition_deploying_to_live_applies(session, seeded_user, _neutralize_side_effects):
    """DEPLOYING → transition(LIVE) → True: state=LIVE, job_events создан, publish вызван."""
    job = await _make_job_in_session(session, seeded_user.id, JobState.DEPLOYING)

    applied = await transition(
        session, job, JobState.LIVE, event_type="state_changed", payload={"live_url": "u"}
    )

    assert applied is True
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.LIVE
    rows = (
        await session.execute(JobEvent.__table__.select().where(JobEvent.job_id == job.id))
    ).all()
    assert len(rows) == 1
    assert rows[0]._mapping["from_state"] == "DEPLOYING"
    assert rows[0]._mapping["to_state"] == "LIVE"
    assert len(_neutralize_side_effects["publishes"]) == 1  # publish на применённом переходе


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (JobState.CREATED, JobState.INTERVIEWING),
        (JobState.INTERVIEWING, JobState.AWAITING_CLARIFICATION),
        (JobState.AWAITING_CLARIFICATION, JobState.SPECCING),
        (JobState.SPECCING, JobState.BUILDING),
        (JobState.BUILDING, JobState.DEPLOYING),
        (JobState.DEPLOYING, JobState.FIXING),
        (JobState.FIXING, JobState.BUILDING),
    ],
)
async def test_all_non_terminal_transitions_apply(session, seeded_user, from_state, to_state):
    """Каждый легитимный переход из НЕ-терминального состояния применяется (CAS 1 строка → True).

    CAS-предикат `state NOT IN (LIVE,FAILED)` НЕ блокирует НЕ-терминальные переходы — барьер ловит
    только терминалы. Регресс-страховка: барьер не сломал нормальную state-machine.
    """
    job = await _make_job_in_session(session, seeded_user.id, from_state)
    applied = await transition(session, job, to_state)
    assert applied is True
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == to_state


async def test_fail_job_on_active_applies_and_counts_once(
    session, seeded_user, _neutralize_side_effects
):
    """fail_job на НЕ-терминальной джобе → применён → job_failed_total +1 (ровно один раз)."""
    job = await _make_job_in_session(session, seeded_user.id, JobState.BUILDING)

    before = _counter_value(
        metrics.job_failed_total, reason="build_unrecoverable", kind="generation"
    )
    await fail_job(session, job, failure_reason="build_unrecoverable")
    after = _counter_value(
        metrics.job_failed_total, reason="build_unrecoverable", kind="generation"
    )

    assert after == before + 1
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.FAILED
    assert refreshed.failure_reason == "build_unrecoverable"


async def test_touch_heartbeat_moves_last_transition_without_state_change(session, seeded_user):
    """touch_heartbeat двигает last_transition_at вперёд, НЕ меняя state и НЕ создавая job_events.

    Витки agent_output_invalid в FIXING (без смены state) двигают heartbeat прогресса (§E2) —
    иначе reconciler ложно помечает живую fix-джобу stuck. Здесь — единичный контракт функции.
    """
    job = await _make_job_in_session(session, seeded_user.id, JobState.FIXING)
    stale = datetime.now(UTC) - timedelta(seconds=10_000)
    job.last_transition_at = stale
    await session.flush()
    before_events = (
        await session.execute(JobEvent.__table__.select().where(JobEvent.job_id == job.id))
    ).all()

    await touch_heartbeat(session, job)
    await session.flush()

    assert job.state == JobState.FIXING  # state НЕ меняется
    assert job.last_transition_at > stale  # heartbeat сдвинут вперёд
    after_events = (
        await session.execute(JobEvent.__table__.select().where(JobEvent.job_id == job.id))
    ).all()
    assert len(after_events) == len(before_events)  # нового job_events нет
