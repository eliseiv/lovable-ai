"""Integration: POST /v1/projects/{pid}/revisions/{n}/rollback — контракт start_rollback.

Реальный Postgres (client шарит тест-сессию); Celery enqueue (_dispatch_rollback) замокан.
Покрывает (ADR-014 §B, docs/06 §S5 Rollback):
  - не-good ревизия → 409; уже-current ревизия → 409;
  - нет такого revision_no → 404; чужой/несуществующий pid → 404 (cross-tenant);
  - квотой НЕ гейтится (rollback не списывает edit/generation квоту);
  - успех → 202 + job_id + target_revision_no; создаётся kind=rollback джоба (минует FIXING).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id, new_revision_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Revision, User
from app.services import project_service

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_rollback_dispatch(monkeypatch):  # noqa: ANN001, ANN202
    """_dispatch_rollback — no-op (без Celery/Docker)."""
    monkeypatch.setattr(project_service, "_dispatch_rollback", lambda *a, **k: None)


async def _user(session, uid, key) -> None:  # noqa: ANN001
    session.add(
        User(
            id=uid,
            api_key_hash=hash_api_key(key),
            monthly_budget_usd=Decimal("50.0000"),
            status="active",
        )
    )
    await session.flush()


async def _project_with_revisions(session, uid, *, second_good=True):  # noqa: ANN001
    """Проект, current = rev2 (good). rev1 — целевая для rollback (good по умолчанию)."""
    pid = new_project_id()
    jid = new_job_id()
    rid1, rid2 = new_revision_id(), new_revision_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    session.add(
        GenerationJob(id=jid, project_id=pid, user_id=uid, state=JobState.LIVE, kind="generation")
    )
    await session.flush()  # job до revisions (FK created_from_job_id)
    session.add(
        Revision(
            id=rid1,
            project_id=pid,
            revision_no=1,
            source_artifact_ref="s3://1",
            created_from_job_id=jid,
            is_good=second_good,
        )
    )
    session.add(
        Revision(
            id=rid2,
            project_id=pid,
            revision_no=2,
            source_artifact_ref="s3://2",
            created_from_job_id=jid,
            is_good=True,
        )
    )
    await session.flush()
    proj = await session.get(Project, pid)
    proj.current_revision_id = rid2
    await session.flush()
    return pid, rid1, rid2


def _hdr(key):  # noqa: ANN001
    return {"Authorization": f"Bearer {key}"}


async def test_rollback_success_202(client, session):
    uid = "u_rb_ok0000000000001"
    await _user(session, uid, "rb-ok-key")
    pid, rid1, _ = await _project_with_revisions(session, uid)
    resp = await client.post(f"/v1/projects/{pid}/revisions/1/rollback", headers=_hdr("rb-ok-key"))
    assert resp.status_code == 202
    body = resp.json()
    assert body["target_revision_no"] == 1
    job_id = body["job_id"]
    # kind=rollback джоба создана (минует FIXING — стартует в CREATED, идёт прямо в DEPLOYING).
    job = await session.get(GenerationJob, job_id)
    assert job.kind == "rollback"
    assert job.state == JobState.CREATED


async def test_rollback_non_good_revision_409(client, session):
    uid = "u_rb_notgood00000001"
    await _user(session, uid, "rb-ng-key")
    # rev1 не good.
    pid, _, _ = await _project_with_revisions(session, uid, second_good=False)
    resp = await client.post(f"/v1/projects/{pid}/revisions/1/rollback", headers=_hdr("rb-ng-key"))
    assert resp.status_code == 409


async def test_rollback_already_current_409(client, session):
    uid = "u_rb_current00000001"
    await _user(session, uid, "rb-cur-key")
    pid, _, _ = await _project_with_revisions(session, uid)
    # rev2 — уже current → 409.
    resp = await client.post(f"/v1/projects/{pid}/revisions/2/rollback", headers=_hdr("rb-cur-key"))
    assert resp.status_code == 409


async def test_rollback_unknown_revision_no_404(client, session):
    uid = "u_rb_norev0000000001"
    await _user(session, uid, "rb-norev-key")
    pid, _, _ = await _project_with_revisions(session, uid)
    resp = await client.post(
        f"/v1/projects/{pid}/revisions/99/rollback", headers=_hdr("rb-norev-key")
    )
    assert resp.status_code == 404


async def test_rollback_cross_tenant_404(client, session, other_user):
    uid = "u_rb_owner0000000001"
    await _user(session, uid, "rb-owner-key")
    pid, _, _ = await _project_with_revisions(session, other_user.id)
    resp = await client.post(
        f"/v1/projects/{pid}/revisions/1/rollback", headers=_hdr("rb-owner-key")
    )
    assert resp.status_code == 404


async def test_rollback_not_gated_by_quota(client, session):
    """Rollback не списывает edit/generation квоту: проходит даже при исчерпанной edit-квоте."""
    from datetime import UTC, datetime

    from app.db.models import EditUsageCounter

    uid = "u_rb_noquota00000001"
    await _user(session, uid, "rb-nq-key")
    pid, _, _ = await _project_with_revisions(session, uid)
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=999))
    await session.flush()
    resp = await client.post(f"/v1/projects/{pid}/revisions/1/rollback", headers=_hdr("rb-nq-key"))
    assert resp.status_code == 202
