"""Integration: auth (401) + cross-tenant изоляция (404) (docs/05-security.md, modules/api)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import GenerationJob, Project

pytestmark = pytest.mark.asyncio


# --- auth 401 ---


async def test_missing_bearer_returns_401(client, seeded_user):
    resp = await client.get("/v1/projects")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_malformed_authorization_returns_401(client, seeded_user):
    resp = await client.get("/v1/projects", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


async def test_invalid_bearer_token_returns_401(client, seeded_user):
    resp = await client.get("/v1/projects", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401


async def test_empty_bearer_returns_401(client, seeded_user):
    resp = await client.get("/v1/projects", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


async def test_valid_bearer_authorizes(client, auth_headers, seeded_user):
    resp = await client.get("/v1/projects", headers=auth_headers)
    assert resp.status_code == 200


# --- cross-tenant 404 ---


async def _make_project_for(session, user_id):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title=None))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=user_id,
            state=JobState.CREATED,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()
    return pid, jid


async def test_owner_sees_only_own_projects(client, auth_headers, session, seeded_user, other_user):
    await _make_project_for(session, seeded_user.id)
    await _make_project_for(session, other_user.id)
    resp = await client.get("/v1/projects", headers=auth_headers)
    assert resp.status_code == 200
    projects = resp.json()["projects"]
    # Видны только проекты владельца ключа.
    assert all(p["prompt"] == "p" for p in projects)
    assert len(projects) == 1


async def test_cross_tenant_project_get_returns_404(
    client, auth_headers, session, seeded_user, other_user
):
    foreign_pid, _ = await _make_project_for(session, other_user.id)
    resp = await client.get(f"/v1/projects/{foreign_pid}", headers=auth_headers)
    assert resp.status_code == 404


async def test_cross_tenant_job_get_returns_404(
    client, auth_headers, session, seeded_user, other_user
):
    _, foreign_jid = await _make_project_for(session, other_user.id)
    resp = await client.get(f"/v1/jobs/{foreign_jid}", headers=auth_headers)
    assert resp.status_code == 404


async def test_own_job_get_returns_200(client, auth_headers, session, seeded_user):
    _, jid = await _make_project_for(session, seeded_user.id)
    resp = await client.get(f"/v1/jobs/{jid}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == jid
