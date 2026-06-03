"""Integration: Celery retry/backoff wiring для инфра-сбоев (ADR-006, docs §D).

Проверяет КОНТРАКТ ретраев на уровне регистрации Celery-таски (без брокера):
- task_build_request / task_deploy / task_fix несут autoretry_for == TRANSIENT_EXCEPTIONS
  (ровно транзиентный набор ADR-006), retry_backoff/jitter/max_retries из retry_policy;
- доменные исключения (ValueError/RuntimeError build-fail) НЕ входят в autoretry_for →
  не ретраятся Celery, идут в FIXING;
- beat-таски (sweeper/reconciler) и happy-path таски зарегистрированы.

Полная retry-EXECUTION (transient→recover без инкремента retry_count; исчерпание
max_retries → FAILED(infra_error)) требует живого Celery-воркера + брокера — это
real-stack E2E (SKIP без окружения, см. tests/e2e). Здесь — что классификатор и
конфиг ретраев согласованы с ADR-006 (autoretry_for именно транзиентный набор).
"""

from __future__ import annotations

import pytest

import app.workers.beat_tasks  # noqa: F401 - регистрирует beat-таски в celery_app
import app.workers.tasks  # noqa: F401 - регистрирует pipeline-таски в celery_app
from app.workers.retry_policy import MAX_RETRIES, TRANSIENT_EXCEPTIONS, is_transient


@pytest.mark.parametrize(
    "task_name", ["pipeline.task_build_request", "pipeline.task_deploy", "pipeline.task_fix"]
)
def test_infra_tasks_autoretry_for_transient_set(task_name: str):
    from app.workers.celery_app import celery_app

    task = celery_app.tasks[task_name]
    autoretry = getattr(task, "autoretry_for", ())
    # autoretry_for должен быть РОВНО транзиентным набором ADR-006 (единый источник).
    assert tuple(autoretry) == TRANSIENT_EXCEPTIONS


@pytest.mark.parametrize(
    "task_name", ["pipeline.task_build_request", "pipeline.task_deploy", "pipeline.task_fix"]
)
def test_infra_tasks_have_backoff_and_max_retries(task_name: str):
    from app.workers.celery_app import celery_app

    task = celery_app.tasks[task_name]
    # retry_backoff/jitter включены, max_retries из retry_policy (ADR-006).
    assert getattr(task, "retry_backoff", False) is True
    assert getattr(task, "retry_jitter", False) is True
    assert getattr(task, "max_retries", None) == MAX_RETRIES


def test_domain_exceptions_excluded_from_autoretry():
    """Доменный build/validation-fail НЕ ретраится Celery (ADR-006 инвариант)."""
    # ValueError/RuntimeError build-fail не входят в транзиентный набор.
    assert ValueError not in TRANSIENT_EXCEPTIONS
    assert is_transient(ValueError("build failed")) is False
    # Generic RuntimeError (не TransientInfraError-подкласс) тоже доменный.
    assert is_transient(RuntimeError("vite exit 1")) is False


def test_acks_late_and_reject_on_worker_lost_configured():
    """acks_late + reject_on_worker_lost (crash-resume, ADR-006/ADR-001)."""
    from app.workers.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_beat_schedule_registers_sweeper_and_reconciler():
    """beat-расписание (docs §E): sweeper уточнений + reconciler застрявших джоб."""
    from app.workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    tasks = {entry["task"] for entry in schedule.values()}
    assert "beat.sweep_clarifications" in tasks
    assert "beat.reconcile_stuck" in tasks
