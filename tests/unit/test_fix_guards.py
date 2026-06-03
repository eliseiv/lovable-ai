"""Unit: четыре гарда fix-loop (docs §C, ADR-005) — чистая функция check_fix_guards.

Проверяется:
  (a) hard cap: retry_count >= max_fix_attempts → build_unrecoverable;
  (b) budget: spend_usd >= budget_usd → budget_exhausted (kill перед LLM-вызовом);
  (c) wall-clock: now >= wall_clock_deadline → wall_clock_exceeded (вкл. naive datetime
      из БД без tzinfo);
  (d) no-progress: та же сигнатура на НОВОМ событии (failure_event_pending) → no_progress;
  порядок проверок (a)→(b)→(c)→(d);
  запись last_failure_signature и сброс failure_event_pending — ТОЛЬКО в гарде (d).

GenerationJob создаётся in-memory (без БД): check_fix_guards читает поля и мутирует
job (намеренный побочный эффект, коммит — у вызывающего).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.enums import JobState
from app.db.models import GenerationJob
from app.pipeline.guards import (
    REASON_BUDGET_EXHAUSTED,
    REASON_BUILD_UNRECOVERABLE,
    REASON_NO_PROGRESS,
    REASON_WALL_CLOCK_EXCEEDED,
    check_fix_guards,
)

SIG_A = "a" * 64
SIG_B = "b" * 64


def _job(
    *,
    retry_count: int = 0,
    max_fix_attempts: int = 3,
    spend: str = "0.0000",
    budget: str = "5.0000",
    deadline: datetime | None = None,
    last_sig: str | None = None,
    pending: bool = False,
) -> GenerationJob:
    return GenerationJob(
        id="j_guardtest0000000000000",
        project_id="p_x",
        user_id="u_x",
        state=JobState.FIXING,
        kind="generation",
        retry_count=retry_count,
        max_fix_attempts=max_fix_attempts,
        spend_usd=Decimal(spend),
        budget_usd=Decimal(budget),
        wall_clock_deadline=deadline,
        last_failure_signature=last_sig,
        failure_event_pending=pending,
    )


# --- (a) hard cap ---


def test_guard_a_hard_cap_trips_at_limit():
    job = _job(retry_count=3, max_fix_attempts=3)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is False
    assert res.reason == REASON_BUILD_UNRECOVERABLE


def test_guard_a_passes_below_limit():
    job = _job(retry_count=2, max_fix_attempts=3, last_sig=None)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is True


# --- (b) budget ---


def test_guard_b_budget_trips_at_or_over():
    job = _job(spend="5.0000", budget="5.0000")
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is False
    assert res.reason == REASON_BUDGET_EXHAUSTED


def test_guard_b_budget_passes_below():
    job = _job(spend="4.9999", budget="5.0000")
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is True


# --- (c) wall-clock ---


def test_guard_c_wall_clock_trips_when_now_past_deadline():
    past = datetime.now(UTC) - timedelta(seconds=1)
    job = _job(deadline=past)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is False
    assert res.reason == REASON_WALL_CLOCK_EXCEEDED


def test_guard_c_wall_clock_passes_before_deadline():
    future = datetime.now(UTC) + timedelta(seconds=3600)
    job = _job(deadline=future)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is True


def test_guard_c_naive_deadline_from_db_treated_as_utc():
    """Naive datetime (из БД без tzinfo) сравнивается как UTC-aware — не падает."""
    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
    job = _job(deadline=naive_past)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is False
    assert res.reason == REASON_WALL_CLOCK_EXCEEDED


def test_guard_c_null_deadline_disabled():
    job = _job(deadline=None)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is True


# --- (d) no-progress: второй distinct failure-event с той же сигнатурой ---


def test_guard_d_first_failure_null_signature_passes():
    """Первый фейл (last_failure_signature IS NULL) — гард пропускает, пишет сигнатуру."""
    job = _job(last_sig=None, pending=True)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is True
    assert job.last_failure_signature == SIG_A
    assert job.failure_event_pending is False  # событие потреблено


def test_guard_d_same_signature_new_event_trips():
    """Вторая та же сигнатура + новое событие (pending=True) → no_progress."""
    job = _job(last_sig=SIG_A, pending=True)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is False
    assert res.reason == REASON_NO_PROGRESS
    # Даже в trip-ветке флаг сброшен — повторная FAILED-доставка идемпотентна.
    assert job.failure_event_pending is False


def test_guard_d_different_signature_passes_and_records():
    """Сигнатура сдвинулась (прогресс) — гард пропускает, перезаписывает сигнатуру."""
    job = _job(last_sig=SIG_A, pending=True)
    res = check_fix_guards(job, failure_signature=SIG_B)
    assert res.ok is True
    assert job.last_failure_signature == SIG_B
    assert job.failure_event_pending is False


def test_guard_d_same_signature_no_pending_is_resume_not_no_progress():
    """Crash-resume: та же сигнатура, но НЕТ нового события (pending=False) → resume.

    Reconciler ре-диспетчеризовал task_fix по тому же логу; событие уже потреблено
    прошлым прогоном гарда → НЕ no_progress, гард пропускает к Agent 4.
    """
    job = _job(last_sig=SIG_A, pending=False)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.ok is True
    assert res.reason is None
    assert job.last_failure_signature == SIG_A


# --- порядок (a)→(b)→(c)→(d) ---


def test_order_hard_cap_precedes_budget():
    """При одновременном исчерпании (a) и (b) возвращается (a) — проверяется первым."""
    job = _job(retry_count=3, max_fix_attempts=3, spend="5.0000", budget="5.0000")
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.reason == REASON_BUILD_UNRECOVERABLE


def test_order_budget_precedes_wall_clock():
    past = datetime.now(UTC) - timedelta(seconds=1)
    job = _job(spend="5.0000", budget="5.0000", deadline=past)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.reason == REASON_BUDGET_EXHAUSTED


def test_order_wall_clock_precedes_no_progress():
    past = datetime.now(UTC) - timedelta(seconds=1)
    job = _job(deadline=past, last_sig=SIG_A, pending=True)
    res = check_fix_guards(job, failure_signature=SIG_A)
    assert res.reason == REASON_WALL_CLOCK_EXCEEDED


def test_signature_not_written_when_early_guard_trips():
    """Запись last_failure_signature — ТОЛЬКО в гарде (d). Ранний гард не перезаписывает."""
    job = _job(retry_count=3, max_fix_attempts=3, last_sig=SIG_B)
    check_fix_guards(job, failure_signature=SIG_A)
    # Гард (a) сработал до (d) → сигнатура не тронута.
    assert job.last_failure_signature == SIG_B
