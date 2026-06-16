"""Integration: cap конкурентных генераций (free=1) + освобождение слота.

docs/modules/auth/03-architecture.md §6, docs/modules/billing/03-architecture.md §4 (S3.5).
free-user с 1 активной джобой → второй POST /projects = 402 reason=concurrency_limit
(каноникализация S3.5: concurrency → 402, НЕ 429; 429 остаётся только за rate-limit, см.
test_rate_limit). Идемпотентный повтор того же ключа НЕ должен считаться против cap; джоба
в терминальном/устойчивом LIVE/FAILED/AWAITING_CLARIFICATION освобождает слот.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.auth.concurrency import count_active_jobs, is_within_concurrency_cap
from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User

pytestmark = pytest.mark.asyncio


async def _capped_user(session) -> User:  # noqa: ANN001
    user = User(
        id="u_capuser000000000000001",
        apple_sub="sub-cap",
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def _add_job(session, user_id: str, state: JobState) -> str:  # noqa: ANN001
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title=None))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=user_id,
            state=state,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()
    return jid


async def test_no_active_jobs_within_cap(session):
    user = await _capped_user(session)
    assert await is_within_concurrency_cap(session, user.id) is True


async def test_one_active_job_exceeds_free_cap(session):
    user = await _capped_user(session)
    await _add_job(session, user.id, JobState.BUILDING)
    # free cap = 1 → одна активная джоба исчерпывает лимит.
    assert await is_within_concurrency_cap(session, user.id) is False


@pytest.mark.parametrize(
    "terminal_state",
    [JobState.LIVE, JobState.FAILED, JobState.AWAITING_CLARIFICATION],
)
async def test_terminal_state_frees_slot(session, terminal_state):
    """Джоба в LIVE/FAILED/AWAITING_CLARIFICATION НЕ активна → слот свободен."""
    user = await _capped_user(session)
    await _add_job(session, user.id, terminal_state)
    assert await count_active_jobs(session, user.id) == 0
    assert await is_within_concurrency_cap(session, user.id) is True


@pytest.mark.parametrize(
    "active_state",
    [
        JobState.CREATED,
        JobState.INTERVIEWING,
        JobState.SPECCING,
        JobState.BUILDING,
        JobState.DEPLOYING,
        JobState.FIXING,
    ],
)
async def test_non_terminal_states_count_as_active(session, active_state):
    user = await _capped_user(session)
    await _add_job(session, user.id, active_state)
    assert await count_active_jobs(session, user.id) == 1
    assert await is_within_concurrency_cap(session, user.id) is False


# --- HTTP-уровень: 2-й POST /projects → 402 (payment-gate), НЕ 429; повтор не считается ---


async def test_second_post_projects_returns_402_not_429(
    client, make_apple_token, patch_apple_jwks, flush_redis, no_side_effects, session
):
    """Второй POST free-юзера блокируется payment-gate как 402 (НЕ 429).

    Для free (max_projects=1 И cap=1) первый POST создаёт 1 проект → второй POST упирается
    в project_limit (gate проверяет его раньше concurrency, docs/billing/03 §4). Контрактный
    инвариант S3.5: блокировка отдаётся как 402 RFC-7807 (reason ∈ payment-причины), а 429
    больше НЕ используется за concurrency. Изоляция именно concurrency_limit — в
    test_billing_quota_gate.test_concurrency_limit_returns_402_not_429.
    """
    r = await client.post(
        "/v1/auth/apple", json={"identity_token": make_apple_token(sub="apple-cap-http")}
    )
    api_key = r.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}", "Idempotency-Key": "cap-key-1"}

    first = await client.post("/v1/projects", data={"prompt": "site one"}, headers=headers)
    assert first.status_code == 202

    headers2 = {"Authorization": f"Bearer {api_key}", "Idempotency-Key": "cap-key-2"}
    second = await client.post("/v1/projects", data={"prompt": "site two"}, headers=headers2)
    assert second.status_code == 402, "S3.5: payment-gate, НЕ 429"
    assert second.headers["content-type"].startswith("application/problem+json")
    # Для free доминирует project_limit (проверяется раньше concurrency_limit, §4).
    assert second.json()["reason"] in {"project_limit", "concurrency_limit"}


async def test_idempotent_repeat_not_counted_against_cap(
    client, make_apple_token, patch_apple_jwks, flush_redis, no_side_effects
):
    """Повтор того же Idempotency-Key возвращает ту же джобу и НЕ упирается в cap (НЕ 402)."""
    r = await client.post(
        "/v1/auth/apple", json={"identity_token": make_apple_token(sub="apple-cap-idem")}
    )
    api_key = r.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}", "Idempotency-Key": "idem-same"}

    first = await client.post("/v1/projects", data={"prompt": "p"}, headers=headers)
    assert first.status_code == 202
    # Тот же ключ → идемпотентный повтор, тот же job_id, НЕ 402.
    again = await client.post("/v1/projects", data={"prompt": "p"}, headers=headers)
    assert again.status_code == 202
    assert again.json()["job_id"] == first.json()["job_id"]
