"""Integration: списание бонус-кредитов на старте генерации (ADR-021 §D, docs/billing §10.3).

Источник истины — docs/modules/billing/03-architecture.md §5 + §10. Проверяет:
- плановая квота тратится ПЕРВОЙ (инкремент usage_counters), пока не исчерпана;
- по исчерпании плана и при balance>0 — декремент bonus_generations_balance на 1 (credit_grants
  списание НЕ создаёт);
- идемпотентность по job_id (общий guard usage_counted покрывает обе ветки — нет двойного
  списания при реплее);
- кредиты НЕ применяются к kind=edit;
- balance не уходит < 0.

free monthly_generations=3 (сидинг plan_quotas). Реальный Postgres (одна тест-сессия).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.billing import usage
from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import CreditGrant, GenerationJob, Project, UsageCounter, User

pytestmark = pytest.mark.asyncio


def _period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


async def _user(session, uid: str, *, balance: int = 0) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
        bonus_generations_balance=balance,
    )
    session.add(user)
    await session.flush()
    return user


async def _job(session, uid: str, *, kind: str = "generation") -> GenerationJob:  # noqa: ANN001
    pid = new_project_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    job = GenerationJob(
        id=new_job_id(),
        project_id=pid,
        user_id=uid,
        state=JobState.CREATED,
        kind=kind,
        budget_usd=Decimal("5.0000"),
    )
    session.add(job)
    await session.flush()
    return job


async def _credit_grant_count(session, uid: str) -> int:  # noqa: ANN001
    return await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.user_id == uid)
    )


async def test_plan_quota_spent_first_while_available(session):
    """Плановая квота не исчерпана → инкремент usage_counters, баланс кредитов НЕ тронут."""
    user = await _user(session, "u_spend_plan00001", balance=5)
    job = await _job(session, user.id)
    applied = await usage.count_generation_start(session, job)
    assert applied is True
    assert await usage.get_usage(session, user.id) == 1
    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 5  # кредит не тронут (план был доступен)


async def test_credit_decremented_when_plan_exhausted(session):
    """План исчерпан (used=3=monthly) + balance>0 → декремент кредита, usage НЕ растёт."""
    user = await _user(session, "u_spend_bonus0001", balance=4)
    session.add(UsageCounter(user_id=user.id, period=_period(), generations_used=3))
    await session.flush()

    job = await _job(session, user.id)
    applied = await usage.count_generation_start(session, job)
    assert applied is True

    assert await usage.get_usage(session, user.id) == 3  # план не инкрементнут
    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 3  # 4 → 3 (списан кредит)
    # Списание кредита НЕ создаёт строку credit_grants (только начисления/коррекции).
    assert await _credit_grant_count(session, user.id) == 0


async def test_spend_idempotent_by_job_id_no_double(session):
    """Повторный count_generation_start того же job_id → no-op (обе ветки под общим guard)."""
    user = await _user(session, "u_spend_idem00001", balance=4)
    session.add(UsageCounter(user_id=user.id, period=_period(), generations_used=3))
    await session.flush()
    job = await _job(session, user.id)

    assert await usage.count_generation_start(session, job) is True
    # Прод-граница: каждый старт — отдельная транзакция, коммитящая job_events-маркер
    # (autoflush=False и в прод-, и в тест-сессии). Flush делает маркер usage_counted видимым
    # для guard'а второго старта (в прод его коммитит транзакция первой таски ДО реплея).
    await session.flush()
    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 3

    # Повтор (acks_late/crash-resume) — баланс не списывается второй раз.
    assert await usage.count_generation_start(session, job) is False
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 3


async def test_credits_not_used_for_edit_kind(session):
    """kind=edit → count_generation_start no-op (кредиты не покрывают правки)."""
    user = await _user(session, "u_spend_edit00001", balance=5)
    job = await _job(session, user.id, kind="edit")
    assert await usage.count_generation_start(session, job) is False
    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 5  # не тронут


async def test_balance_floor_at_zero_falls_back_to_plan_increment(session):
    """План исчерпан и balance==0 → кредита нет, баланс не уходит < 0 (fallback план-инкремент)."""
    user = await _user(session, "u_spend_floor0001", balance=0)
    session.add(UsageCounter(user_id=user.id, period=_period(), generations_used=3))
    await session.flush()
    job = await _job(session, user.id)

    applied = await usage.count_generation_start(session, job)
    assert applied is True
    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 0  # не < 0
    # Кредитов не было → fallback на плановый инкремент (docs §10.3 race-ветка).
    assert await usage.get_usage(session, user.id) == 4
