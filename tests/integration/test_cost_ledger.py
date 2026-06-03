"""Integration: cost-ledger — record_usage пишет llm_usage и аккумулирует spend_usd."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import GenerationJob, LlmUsage, Project
from app.pipeline.agents.claude_client import AgentCall
from app.pipeline.cost import record_usage

pytestmark = pytest.mark.asyncio


async def _make_job(session, user_id):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title=None))
    job = GenerationJob(
        id=jid,
        project_id=pid,
        user_id=user_id,
        state=JobState.SPECCING,
        kind="generation",
        budget_usd=Decimal("5.0000"),
        spend_usd=Decimal("0.0000"),
    )
    session.add(job)
    await session.flush()
    return job


def _call(cost, cache_read=11, cache_write=7):  # noqa: ANN001, ANN201
    return AgentCall(
        text="x",
        model="claude-opus-4-8",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=Decimal(cost),
    )


async def test_record_usage_writes_row_with_cache_tokens(session, seeded_user):
    job = await _make_job(session, seeded_user.id)
    await record_usage(session, job, "agent1", _call("0.1234"))
    await session.flush()
    rows = (
        (await session.execute(select(LlmUsage).where(LlmUsage.job_id == job.id))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].cache_read_tokens == 11
    assert rows[0].cache_write_tokens == 7
    assert rows[0].cost_usd == Decimal("0.1234")
    assert rows[0].agent == "agent1"


async def test_spend_accumulates_across_agent_calls(session, seeded_user):
    job = await _make_job(session, seeded_user.id)
    await record_usage(session, job, "agent1", _call("0.1000"))
    await record_usage(session, job, "agent2", _call("0.2500"))
    await record_usage(session, job, "agent3", _call("0.0500"))
    await session.flush()
    assert job.spend_usd == Decimal("0.4000")
    n = await session.scalar(
        select(func.count()).select_from(LlmUsage).where(LlmUsage.job_id == job.id)
    )
    assert n == 3


async def test_usage_recorded_even_for_invalid_agent3_output(session, seeded_user):
    """Cost-ledger: usage Agent 3 учитывается даже при невалидном output (вызов оплачен).

    Воспроизводит ветку tasks._spec: AgentOutputError несёт call → record_usage до fail.
    """
    from app.schemas.agent_output import AgentOutputError

    job = await _make_job(session, seeded_user.id)
    invalid_call = _call("0.3300")
    exc = AgentOutputError("bad tree", signature="too_many_files", call=invalid_call)
    # Логика tasks._spec: if isinstance(exc.call, AgentCall): record_usage(...)
    assert isinstance(exc.call, AgentCall)
    await record_usage(session, job, "agent3", exc.call)
    await session.flush()
    rows = (
        (
            await session.execute(
                select(LlmUsage).where(LlmUsage.job_id == job.id, LlmUsage.agent == "agent3")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].cost_usd == Decimal("0.3300")
    assert job.spend_usd == Decimal("0.3300")
