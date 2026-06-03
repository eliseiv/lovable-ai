"""Integration: quota-gate на POST /v1/projects → 402 RFC-7807 (docs/billing/02 §3, 03 §4).

Каждый reason: no_entitlement (expired/billing_issue без активного уровня); quota_exhausted
(generations≥monthly); project_limit (projects≥max_projects); concurrency_limit (активных≥
cap). free-дефолт без подписки проходит. Ответ — application/problem+json со status=402,
reason, required_entitlement. 429 остаётся только за rate-limit (см. test_rate_limit).

Реальный Postgres (client шарит тест-сессию). Auth — legacy-ключ (api_key_hash) для
детерминизма; dispatch/publish мокаются (no_side_effects).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id, new_subscription_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Subscription, UsageCounter, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str, key: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(key),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def _sub(session, uid: str, *, access_level: str, status: str) -> None:  # noqa: ANN001
    session.add(
        Subscription(
            id=new_subscription_id(),
            user_id=uid,
            access_level=access_level,
            status=status,
            will_renew=True,
            raw={},
            synced_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _add_active_job(session, uid: str) -> None:  # noqa: ANN001
    pid = new_project_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    session.add(
        GenerationJob(
            id=new_job_id(),
            project_id=pid,
            user_id=uid,
            state=JobState.BUILDING,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()


async def _add_project(session, uid: str) -> None:  # noqa: ANN001
    session.add(Project(id=new_project_id(), user_id=uid, prompt="existing", title=None))
    await session.flush()


def _hdr(key: str, idem: str = "qg-key") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Idempotency-Key": idem}


async def _post(client, key, idem="qg-key"):  # noqa: ANN001
    return await client.post("/v1/projects", json={"prompt": "site"}, headers=_hdr(key, idem))


# --- free-дефолт без подписки проходит ---


async def test_free_default_no_subscription_passes(client, session, no_side_effects):
    user = await _user(session, "u_qg_free00000000001", "qg-free-key")
    resp = await _post(client, "qg-free-key")
    assert resp.status_code == 202
    assert resp.json()["job_id"].startswith("j_")
    assert user.id  # smoke


# --- no_entitlement: expired / billing_issue ---


@pytest.mark.parametrize("status", ["expired", "billing_issue"])
async def test_no_entitlement_for_inactive_status(client, session, no_side_effects, status):
    key = f"qg-noent-{status}"
    await _user(session, f"u_qg_{status[:6]}0000001", key)
    await _sub(session, f"u_qg_{status[:6]}0000001", access_level="pro", status=status)
    resp = await _post(client, key)
    assert resp.status_code == 402
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 402
    assert body["reason"] == "no_entitlement"
    assert body["required_entitlement"] == "pro"


# --- quota_exhausted ---


async def test_quota_exhausted_when_generations_at_monthly(client, session, no_side_effects):
    key = "qg-quota-key"
    uid = "u_qg_quota0000000001"
    await _user(session, uid, key)
    # free monthly_generations = 3 → выставляем used=3.
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(UsageCounter(user_id=uid, period=period, generations_used=3))
    await session.flush()
    resp = await _post(client, key)
    assert resp.status_code == 402
    assert resp.json()["reason"] == "quota_exhausted"


# --- project_limit ---


async def test_project_limit_when_projects_at_max(client, session, no_side_effects):
    key = "qg-proj-key"
    uid = "u_qg_proj00000000001"
    await _user(session, uid, key)
    # free max_projects = 1 → уже есть 1 проект.
    await _add_project(session, uid)
    resp = await _post(client, key)
    assert resp.status_code == 402
    assert resp.json()["reason"] == "project_limit"


# --- concurrency_limit (402, НЕ 429) ---


async def test_concurrency_limit_returns_402_not_429(client, session, no_side_effects):
    key = "qg-conc-key"
    uid = "u_qg_conc00000000001"
    await _user(session, uid, key)
    # pro: max_projects=null (project_limit не сработает), cap=3 → 3 активные джобы исчерпывают.
    # Изолируем именно concurrency_limit (gate проверяет project_limit раньше concurrency).
    await _sub(session, uid, access_level="pro", status="active")
    for _ in range(3):
        await _add_active_job(session, uid)
    resp = await _post(client, key)
    assert resp.status_code == 402
    assert resp.json()["reason"] == "concurrency_limit"


async def test_pro_user_passes_with_active_subscription(client, session, no_side_effects):
    key = "qg-pro-key"
    uid = "u_qg_pro000000000001"
    await _user(session, uid, key)
    await _sub(session, uid, access_level="pro", status="active")
    # pro: max_projects=null, cap=3 — проходит.
    resp = await _post(client, key)
    assert resp.status_code == 202


async def test_grace_status_passes_gate(client, session, no_side_effects):
    key = "qg-grace-key"
    uid = "u_qg_grace000000001"
    await _user(session, uid, key)
    await _sub(session, uid, access_level="pro", status="grace")
    resp = await _post(client, key)
    assert resp.status_code == 202
