"""ADR-029 §B — re-read state-guard в _deploy перед записью LIVE (барьер B).

Нормативный источник — docs/adr/ADR-029-terminal-state-invariant-no-overwrite-deploy-guard-
reconciler-revoke.md §Decision B, docs/06-testing-strategy.md §Integration «reconciler пометил
FAILED → _deploy НЕ пишет LIVE», docs/modules/pipeline/03-architecture.md §Инвариант
терминальности; app/workers/tasks.py::_deploy (session.refresh(job,['state']) перед LIVE).

Сценарий прод-инцидента: docker run + wait_until_live длятся минуты; in-memory job.state остался
DEPLOYING с момента загрузки, но reconciler в ОТДЕЛЬНОЙ сессии записал FAILED(stuck_timeout/
wall_clock_exceeded). Перед финальной записью LIVE _deploy ПЕРЕЧИТЫВАЕТ job.state из БД
(session.refresh) и пишет LIVE ТОЛЬКО если джоба ещё DEPLOYING. Иначе — снимает deploy-контейнер
teardown-инвариантом, выставляет deployment.status='failed' и НЕ пишет LIVE (итог FAILED).

Реальный Postgres (autonomous_db/session_scope). docker/health мокаются. Гонку моделируем:
health-мок переводит джобу в FAILED в ОТДЕЛЬНОЙ транзакции ПЕРЕД возвратом ok=True — точная
имитация reconciler'а, отработавшего во время длинного wait_until_live.

Покрывает чек-лист (docs §49):
- reconciler пометил FAILED во время _deploy → _deploy НЕ оставляет LIVE (итог FAILED);
- deploy-контейнер снят teardown-инвариантом, deployment.status='failed';
- happy: state==DEPLOYING (без гонки) → LIVE записан, deployment active.
"""

from __future__ import annotations

import io
import tarfile
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.storage.s3 as s3
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import (
    Answer,
    GenerationJob,
    JobEvent,
    LlmUsage,
    Project,
    Question,
    Revision,
    SiteDeployment,
    User,
)
from app.db.session import session_scope
from app.pipeline.events import fail_job

pytestmark = pytest.mark.asyncio

UID = "u_adr029deployowner00"


class _FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key, data, content_type="application/octet-stream"):  # noqa: ANN001, ANN202
        self.objects[key] = data
        return key

    async def put_text(self, key, text, content_type="text/plain"):  # noqa: ANN001, ANN202
        self.objects[key] = text.encode("utf-8")
        return key

    async def get_bytes(self, key):  # noqa: ANN001, ANN202
        return self.objects[key]


def _dist_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"<html><body>hi</body></html>"
        info = tarfile.TarInfo(name="index.html")
        info.size = len(data)
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _purge() -> None:
    async with session_scope() as s:
        jids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == UID)
                    )
                )
                .scalars()
                .all()
            )
        )
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        if pids:
            await s.execute(delete(SiteDeployment).where(SiteDeployment.project_id.in_(pids)))
        if jids:
            await s.execute(delete(LlmUsage).where(LlmUsage.job_id.in_(jids)))
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(jids)))
            await s.execute(delete(Answer).where(Answer.job_id.in_(jids)))
            await s.execute(delete(Question).where(Question.job_id.in_(jids)))
            await s.execute(delete(Revision).where(Revision.created_from_job_id.in_(jids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest_asyncio.fixture
async def deploying_job(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    """Committed user+project+job в DEPLOYING. publish/push/dispatch нейтрализованы."""
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)
    pid = new_project_id()
    jid = new_job_id()
    await _purge()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("adr029d-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="Landing", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=JobState.DEPLOYING,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
            )
        )
        await s.commit()
    yield pid, jid
    await _purge()


def _wire_common(monkeypatch, storage):  # noqa: ANN001, ANN202
    import app.pipeline.events as events
    import app.storage.s3 as s3mod
    import app.workers.tasks as tasks

    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop_publish)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    monkeypatch.setattr(
        tasks.docker_deploy, "publish_dist", lambda s, project_id, dist_dir: dist_dir
    )
    return tasks


async def _seed_dist(storage, jid):  # noqa: ANN001, ANN202
    storage.objects[s3.dist_key(jid)] = _dist_tgz()


async def _read_state(jid):  # noqa: ANN001, ANN202
    async with session_scope() as s:
        return await s.scalar(select(GenerationJob.state).where(GenerationJob.id == jid))


# ---------------------------------------------------------------------------
# 1. reconciler пометил FAILED во время _deploy → _deploy НЕ пишет LIVE (барьер B).
# ---------------------------------------------------------------------------


async def test_deploy_reread_guard_does_not_overwrite_failed(deploying_job, monkeypatch):
    """Reconciler/fail_job записал FAILED во время длинного wait_until_live → _deploy re-read
    видит state != DEPLOYING → teardown + status=failed, LIVE НЕ записан, итог FAILED.

    Точная имитация прод-инцидента j_kthn...: живая task_deploy добежала после wait_until_live,
    но reconciler уже терминализировал джобу. Барьер B (session.refresh job.state) ловит это ДО
    записи LIVE.
    """
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return docker_deploy.DeployResult(
            container_id="cid_live", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    teardown_calls: list[str] = []
    monkeypatch.setattr(
        tasks.docker_deploy, "teardown_container", lambda cn: teardown_calls.append(cn)
    )

    async def _health_then_reconciler_fails(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        # Гонка: пока шёл wait_until_live, reconciler в ОТДЕЛЬНОЙ транзакции записал FAILED.
        # Делаем это ПЕРЕД возвратом ok=True — _deploy затем re-read'нет state и увидит FAILED.
        async with session_scope() as s2:
            job2 = await s2.get(GenerationJob, jid)
            await fail_job(s2, job2, failure_reason="wall_clock_exceeded")
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _health_then_reconciler_fails)

    await tasks._deploy(jid)

    # LIVE НЕ записан: терминал FAILED (записанный reconciler'ом первым) победил.
    assert await _read_state(jid) == JobState.FAILED
    # Deploy-контейнер снят teardown-инвариантом (как project_deleted-ветка).
    assert teardown_calls == ["site_" + (await _read_subdomain(pid))]
    async with session_scope() as s:
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "failed"  # status=failed, не active
        proj = await s.get(Project, pid)
        assert proj.current_revision_id is None  # LIVE-побочки (current_revision_id) не записаны


# ---------------------------------------------------------------------------
# 2. happy: state==DEPLOYING (без гонки) → LIVE записан, deployment active.
# ---------------------------------------------------------------------------


async def test_deploy_writes_live_when_state_still_deploying(deploying_job, monkeypatch):
    """Без гонки (state остаётся DEPLOYING при re-read) → LIVE записан, deployment active.

    Регресс-страховка: барьер B НЕ ломает нормальный happy-path деплоя (re-read видит DEPLOYING
    → пишет LIVE как обычно).
    """
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return docker_deploy.DeployResult(
            container_id="cid_live", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)
    teardown_calls: list[str] = []
    monkeypatch.setattr(
        tasks.docker_deploy, "teardown_container", lambda cn: teardown_calls.append(cn)
    )

    async def _fake_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health)

    await tasks._deploy(jid)

    assert await _read_state(jid) == JobState.LIVE
    assert teardown_calls == []  # happy-path не делает teardown
    async with session_scope() as s:
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "active"
        assert dep.container_id == "cid_live"
        proj = await s.get(Project, pid)
        assert proj.current_revision_id is not None


async def _read_subdomain(pid: str) -> str:
    async with session_scope() as s:
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        return dep.subdomain
