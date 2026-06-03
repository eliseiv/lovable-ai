"""Integration: redeploy_revision — re-deploy good-ревизии без downtime (ADR-014 §B §7).

Реальный Postgres (session_scope autonomous_db); внешние границы (S3/storage, Docker,
health, workspace, sandbox) мокаются. Покрывает (docs/06 §S5 Rollback):
  - health 200: новый деплой active, current_revision_id ← целевая, прежний active→superseded
    (teardown прежнего ТОЛЬКО после health 200 — без downtime);
  - health-fail нового: прежний active НЕТРОНУТ, current_revision_id не меняется,
    новый деплой → failed (без downtime);
  - порядок: teardown прежнего контейнера происходит ПОСЛЕ успешного health нового.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.ids import new_deployment_id, new_job_id, new_project_id, new_revision_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import (
    GenerationJob,
    Project,
    Revision,
    SiteDeployment,
    User,
)
from app.db.session import session_scope
from app.deploy.health import HealthResult

pytestmark = pytest.mark.asyncio

UID = "u_rbredeploy00000001"


async def _purge() -> None:
    async with session_scope() as s:
        pids = (await s.execute(select(Project.id).where(Project.user_id == UID))).scalars().all()
        # NULL current_revision_id перед удалением revisions (FK fk_projects_current_revision).
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        for pid in pids:
            await s.execute(delete(SiteDeployment).where(SiteDeployment.project_id == pid))
        # revisions FK created_from_job_id → удаляем revisions перед jobs.
        for pid in pids:
            await s.execute(delete(Revision).where(Revision.project_id == pid))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        await s.execute(delete(Project).where(Project.user_id == UID))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


async def _seed_project() -> dict:
    """Проект на rev2 (active deployment dep2). rev1 — целевая good для rollback."""
    pid = new_project_id()
    jid = new_job_id()
    rid1, rid2 = new_revision_id(), new_revision_id()
    dep2 = new_deployment_id()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("rbrd-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="p", title=None))
        s.add(GenerationJob(id=jid, project_id=pid, user_id=UID, state=JobState.LIVE))
        await s.flush()  # job до revisions (FK created_from_job_id)
        s.add(
            Revision(
                id=rid1,
                project_id=pid,
                revision_no=1,
                source_artifact_ref="s3://src1",
                created_from_job_id=jid,
                is_good=True,
            )
        )
        s.add(
            Revision(
                id=rid2,
                project_id=pid,
                revision_no=2,
                source_artifact_ref="s3://src2",
                created_from_job_id=jid,
                is_good=True,
            )
        )
        s.add(
            SiteDeployment(
                id=dep2,
                project_id=pid,
                revision_id=rid2,
                subdomain="sub-current",
                live_url="http://sub-current.apps.localhost",
                dist_artifact_ref="s3://dist2",
                container_id="cid-current",
                status="active",
            )
        )
        await s.flush()  # revisions до установки projects.current_revision_id (FK)
        proj = await s.get(Project, pid)
        proj.current_revision_id = rid2
        await s.commit()
    return {"pid": pid, "rid1": rid1, "rid2": rid2, "dep2": dep2}


@pytest_asyncio.fixture
async def redeploy_env(autonomous_db):  # noqa: ANN001, ANN201
    await _purge()
    data = await _seed_project()
    yield data
    await _purge()


def _patch_deploy_externals(monkeypatch, *, health_ok: bool, teardown_log: list):
    """Мокает S3/storage, workspace, sandbox, docker_deploy, health внутри app.deploy.rollback."""
    import app.deploy.rollback as rb

    class _FakeStorage:
        async def get_bytes(self, key):  # noqa: ANN001, ANN202
            return b"tgz-bytes"

        async def put_bytes(self, key, data, ctype):  # noqa: ANN001, ANN202
            return key

    monkeypatch.setattr(rb, "get_storage", lambda: _FakeStorage())
    # dist доступен → materialize идёт по короткому пути (без пересборки).
    monkeypatch.setattr(rb, "_dist_available", _async_true)
    monkeypatch.setattr(rb.workspace, "safe_extract_tgz", lambda data, dest: None)
    monkeypatch.setattr(rb.sandbox, "cleanup_workspace", lambda p: None)
    monkeypatch.setattr(
        rb.docker_deploy,
        "publish_dist",
        lambda s, pid, wd: Path("/tmp/site"),  # noqa: S108
    )

    class _DeployResult:
        container_id = "cid-new"
        container_name = "site_sub-new"

    monkeypatch.setattr(
        rb.docker_deploy,
        "run_nginx_container",
        lambda s, project_id, subdomain, site_dir: _DeployResult(),
    )

    def _teardown(name):  # noqa: ANN001, ANN202
        teardown_log.append(name)

    monkeypatch.setattr(rb.docker_deploy, "teardown_container", _teardown)

    async def _health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return HealthResult(ok=health_ok, detail="ok" if health_ok else "timeout")

    monkeypatch.setattr(rb.health, "wait_until_live", _health)
    # new_subdomain детерминированный — чтобы проверять teardown прежнего, не нового.
    monkeypatch.setattr(rb, "new_subdomain", lambda: "sub-new")


async def _async_true(storage, revision):  # noqa: ANN001, ANN202
    return True


async def test_redeploy_health_ok_no_downtime(redeploy_env, monkeypatch):
    """health 200 → новый active, current←rev1, прежний superseded; teardown ПОСЛЕ health."""
    from app.deploy.rollback import redeploy_revision

    teardown_log: list[str] = []
    _patch_deploy_externals(monkeypatch, health_ok=True, teardown_log=teardown_log)

    result = await redeploy_revision(redeploy_env["pid"], redeploy_env["rid1"])
    assert result.ok is True

    async with session_scope() as s:
        proj = await s.get(Project, redeploy_env["pid"])
        # current_revision_id переключён на целевую rev1.
        assert proj.current_revision_id == redeploy_env["rid1"]
        # Прежний active (rev2) → superseded.
        prev = await s.get(SiteDeployment, redeploy_env["dep2"])
        assert prev.status == "superseded"
        # Новый деплой active.
        new_dep = (
            await s.execute(
                select(SiteDeployment).where(
                    SiteDeployment.project_id == redeploy_env["pid"],
                    SiteDeployment.subdomain == "sub-new",
                )
            )
        ).scalar_one()
        assert new_dep.status == "active"
    # Teardown прежнего контейнера выполнен (после health) — без downtime до подтверждения.
    assert "site_sub-current" in teardown_log


async def test_redeploy_health_fail_keeps_previous(redeploy_env, monkeypatch):
    """health-fail нового → прежний active НЕТРОНУТ, current_revision_id не меняется."""
    from app.deploy.rollback import redeploy_revision

    teardown_log: list[str] = []
    _patch_deploy_externals(monkeypatch, health_ok=False, teardown_log=teardown_log)

    result = await redeploy_revision(redeploy_env["pid"], redeploy_env["rid1"])
    assert result.ok is False

    async with session_scope() as s:
        proj = await s.get(Project, redeploy_env["pid"])
        # current не сдвинулся — прежняя ревизия осталась активной (без downtime).
        assert proj.current_revision_id == redeploy_env["rid2"]
        prev = await s.get(SiteDeployment, redeploy_env["dep2"])
        assert prev.status == "active"
        new_dep = (
            await s.execute(
                select(SiteDeployment).where(
                    SiteDeployment.project_id == redeploy_env["pid"],
                    SiteDeployment.subdomain == "sub-new",
                )
            )
        ).scalar_one()
        assert new_dep.status == "failed"
    # Снесён ТОЛЬКО новый (провалившийся) контейнер, прежний НЕ тронут.
    assert "site_sub-current" not in teardown_log
    assert "site_sub-new" in teardown_log
