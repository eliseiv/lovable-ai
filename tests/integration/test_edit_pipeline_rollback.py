"""Integration: edit-цикл — авто-rollback при исчерпании fix-гарда (ADR-014 §C, docs/06 §S5).

Реальный Postgres (session_scope autonomous_db); redeploy_revision (Docker/S3/health)
мокается. Покрывает финализацию неудачной правки через _finalize_fix_failure:
  - kind='edit' + исчерпание гарда → _auto_rollback_edit → передеплой прежней good-ревизии
    + FAILED(edit_failed_rolled_back); сайт остаётся LIVE на прежней ревизии;
  - kind='generation' → штатный FAILED(reason гарда), НЕ edit_failed_rolled_back (контраст);
  - нет прежней good-ревизии → FAILED(edit_failed_rolled_back) без передеплоя.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.ids import new_job_id, new_project_id, new_revision_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, Revision, User
from app.db.session import session_scope
from app.workers import tasks as worker_tasks

pytestmark = pytest.mark.asyncio

UID = "u_editrb000000000001"


async def _purge() -> None:
    async with session_scope() as s:
        pids = (await s.execute(select(Project.id).where(Project.user_id == UID))).scalars().all()
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        # NULL current_revision_id перед удалением revisions (FK fk_projects_current_revision).
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        for pid in pids:
            await s.execute(delete(Revision).where(Revision.project_id == pid))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        await s.execute(delete(Project).where(Project.user_id == UID))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


async def _seed(kind: str, *, with_good_base: bool = True) -> dict:
    """Проект LIVE на good-ревизии rev1 + edit/generation-джоба в FIXING.

    edit_requested-событие указывает base_revision_id=rev1 (источник правки).
    """
    pid = new_project_id()
    src_jid = new_job_id()
    edit_jid = new_job_id()
    rid1 = new_revision_id()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("editrb-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="p", title=None))
        s.add(
            GenerationJob(
                id=src_jid, project_id=pid, user_id=UID, state=JobState.LIVE, kind="generation"
            )
        )
        s.add(
            GenerationJob(
                id=edit_jid, project_id=pid, user_id=UID, state=JobState.FIXING, kind=kind
            )
        )
        await s.flush()  # project/jobs до revision (FK created_from_job_id)
        s.add(
            Revision(
                id=rid1,
                project_id=pid,
                revision_no=1,
                source_artifact_ref="s3://1",
                created_from_job_id=src_jid,
                is_good=with_good_base,
            )
        )
        await s.flush()
        proj = await s.get(Project, pid)
        proj.current_revision_id = rid1 if with_good_base else None
        # edit_requested: источник истины инструкции/базовой ревизии (читает _load_edit_request).
        from app.pipeline.events import record_event

        await record_event(
            s,
            edit_jid,
            "edit_requested",
            payload={"instruction": "make it blue", "base_revision_id": rid1},
        )
        await s.commit()
    return {"pid": pid, "edit_jid": edit_jid, "rid1": rid1}


@pytest_asyncio.fixture
async def edit_rb_env(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    await _purge()
    # redeploy_revision — мок (без Docker/S3/health). Авто-rollback не должен реально деплоить.
    calls: list = []

    async def _fake_redeploy(project_id, revision_id):  # noqa: ANN001, ANN202
        from app.deploy.rollback import RedeployResult

        calls.append((project_id, revision_id))
        return RedeployResult(ok=True, detail="ok", subdomain="s", live_url="http://x")

    import app.deploy.rollback as rb

    monkeypatch.setattr(rb, "redeploy_revision", _fake_redeploy)
    # _auto_rollback_edit импортирует redeploy_revision лениво из app.deploy.rollback — патч там.
    yield {"calls": calls}
    await _purge()


async def test_edit_guard_exhaustion_auto_rollback(edit_rb_env):
    data = await _seed("edit", with_good_base=True)
    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        rolled_back = await worker_tasks._finalize_fix_failure(
            s, job, failure_reason="build_unrecoverable", signature="sig"
        )
        await s.commit()
    assert rolled_back is True

    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        # Edit-джоба FAILED с reason edit_failed_rolled_back.
        assert job.state == JobState.FAILED
        assert job.failure_reason == "edit_failed_rolled_back"


async def test_generation_guard_exhaustion_plain_failed(edit_rb_env):
    data = await _seed("generation", with_good_base=True)
    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        rolled_back = await worker_tasks._finalize_fix_failure(
            s, job, failure_reason="build_unrecoverable", signature="sig"
        )
        await s.commit()
    # generation: НЕ rollback.
    assert rolled_back is False

    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        assert job.state == JobState.FAILED
        assert job.failure_reason == "build_unrecoverable"


async def test_edit_no_good_base_fails_rolled_back_without_redeploy(edit_rb_env):
    """Нет прежней good-ревизии → edit_failed_rolled_back без передеплоя (сайт уже на good)."""
    data = await _seed("edit", with_good_base=False)
    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        rolled_back = await worker_tasks._finalize_fix_failure(
            s, job, failure_reason="no_progress", signature="sig"
        )
        await s.commit()
    assert rolled_back is True
    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        assert job.failure_reason == "edit_failed_rolled_back"
    # redeploy не вызван (нет base-ревизии).
    assert edit_rb_env["calls"] == []
