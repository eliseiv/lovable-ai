"""Integration: edit_usage_counters инкремент (Sprint 5, ADR-014 §A, docs/billing §7).

Реальный Postgres (session fixture). Покрывает:
  - count_edit_start: инкремент edits_used на edit-джобе, идемпотентно по job_id
    (повтор → no-op, job_events-маркер edit_usage_counted);
  - count_edit_start НЕ трогает usage_counters (generations_used);
  - count_generation_start НЕ трогает edit_usage_counters;
  - count_edit_start no-op для kind != 'edit'; count_generation_start no-op для kind!='generation'.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.billing import usage
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid) -> None:  # noqa: ANN001
    session.add(
        User(
            id=uid,
            api_key_hash=hash_api_key(f"{uid}-k"),
            monthly_budget_usd=Decimal("50.0000"),
            status="active",
        )
    )
    await session.flush()


async def _job(session, uid, kind) -> GenerationJob:  # noqa: ANN001
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    job = GenerationJob(id=jid, project_id=pid, user_id=uid, state=JobState.CREATED, kind=kind)
    session.add(job)
    await session.flush()
    return job


async def test_count_edit_start_increments(session):
    uid = "u_eu_inc000000000001"
    await _user(session, uid)
    job = await _job(session, uid, "edit")
    applied = await usage.count_edit_start(session, job)
    await session.flush()
    assert applied is True
    assert await usage.get_edit_usage(session, uid) == 1


async def test_count_edit_start_idempotent_by_job_id(session):
    uid = "u_eu_idem00000000001"
    await _user(session, uid)
    job = await _job(session, uid, "edit")
    assert await usage.count_edit_start(session, job) is True
    await session.flush()
    # Повтор того же job_id → no-op (guard job_events edit_usage_counted).
    assert await usage.count_edit_start(session, job) is False
    await session.flush()
    assert await usage.get_edit_usage(session, uid) == 1


async def test_edit_start_does_not_touch_generation_usage(session):
    uid = "u_eu_iso0000000001a"
    await _user(session, uid)
    job = await _job(session, uid, "edit")
    await usage.count_edit_start(session, job)
    await session.flush()
    # generations_used не тронут отдельным счётчиком правок.
    assert await usage.get_usage(session, uid) == 0
    assert await usage.get_edit_usage(session, uid) == 1


async def test_generation_start_does_not_touch_edit_usage(session):
    uid = "u_eu_iso0000000001b"
    await _user(session, uid)
    job = await _job(session, uid, "generation")
    await usage.count_generation_start(session, job)
    await session.flush()
    assert await usage.get_usage(session, uid) == 1
    assert await usage.get_edit_usage(session, uid) == 0


async def test_count_edit_start_noop_for_generation_kind(session):
    uid = "u_eu_kind00000000001"
    await _user(session, uid)
    job = await _job(session, uid, "generation")
    assert await usage.count_edit_start(session, job) is False
    await session.flush()
    assert await usage.get_edit_usage(session, uid) == 0


async def test_count_generation_start_noop_for_edit_kind(session):
    uid = "u_eu_kind00000000002"
    await _user(session, uid)
    job = await _job(session, uid, "edit")
    assert await usage.count_generation_start(session, job) is False
    await session.flush()
    assert await usage.get_usage(session, uid) == 0
