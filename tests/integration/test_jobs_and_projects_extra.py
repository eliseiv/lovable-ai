"""Integration: добавочное покрытие jobs/projects роутеров (live_url, list, readyz)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.ids import new_deployment_id, new_job_id, new_project_id, new_revision_id
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Revision, SiteDeployment

pytestmark = pytest.mark.asyncio


async def _live_project(session, user_id):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    rid = new_revision_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title="Cafe"))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=user_id,
            state=JobState.LIVE,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()
    session.add(
        Revision(
            id=rid,
            project_id=pid,
            revision_no=1,
            source_artifact_ref="sources/x/source.tgz",
            created_from_job_id=jid,
            is_good=True,
        )
    )
    await session.flush()
    session.add(
        SiteDeployment(
            id=new_deployment_id(),
            project_id=pid,
            revision_id=rid,
            subdomain="abcdef0123456789",
            live_url="http://abcdef0123456789.apps.localhost/",
            dist_artifact_ref="dist/x/dist.tgz",
            status="active",
        )
    )
    await session.flush()
    return pid, jid


async def test_get_job_live_includes_live_url(client, auth_headers, session, seeded_user):
    _, jid = await _live_project(session, seeded_user.id)
    resp = await client.get(f"/v1/jobs/{jid}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "LIVE"
    assert body["live_url"] == "http://abcdef0123456789.apps.localhost/"


async def test_list_projects_includes_live_url(client, auth_headers, session, seeded_user):
    pid, _ = await _live_project(session, seeded_user.id)
    resp = await client.get("/v1/projects", headers=auth_headers)
    assert resp.status_code == 200
    projects = resp.json()["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == pid
    assert projects[0]["live_url"] == "http://abcdef0123456789.apps.localhost/"


async def test_get_project_by_id_owner(client, auth_headers, session, seeded_user):
    pid, _ = await _live_project(session, seeded_user.id)
    resp = await client.get(f"/v1/projects/{pid}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


async def test_questions_empty_when_none(client, auth_headers, session, seeded_user):
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=seeded_user.id, prompt="p", title=None))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=seeded_user.id,
            state=JobState.CREATED,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()
    resp = await client.get(f"/v1/jobs/{jid}/questions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["questions"] == []


async def test_readyz_reports_postgres_and_redis(client, seeded_user):
    """readyz: реальные Postgres+Redis доступны → 200 ready."""
    resp = await client.get("/readyz")
    # Postgres (тест-сессия) ok; Redis — реальный тест-Redis из conftest.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
