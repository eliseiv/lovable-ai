"""Integration: DEPLOYING → FIXING — запись failure_log (§F) + классификация (§B/§F).

Реальный Postgres. Внешние границы (docker/health/S3/build) — моки.

Покрывает:
- build-fail (task_build_request) → FIXING: failure_log в logs/{job_id}/build.log с
  машинной шапкой (§F), failure_log_ref записан, failure_event_pending=True;
- deploy-fail (docker run) → FIXING с failure_class=deploy_error (НЕ health_timeout);
- health-fail (timeout) → FIXING с failure_class=health_timeout;
- health 5xx/4xx классификация;
- teardown ДО входа в FIXING (deploy/health);
- npm vs build классификация build-fail.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

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
from app.pipeline.failure_signature import parse_failure_log
from app.storage import s3

pytestmark = pytest.mark.asyncio

UID = "u_d2fowner000000000000"


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
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"<html>hi</html>"
        info = tarfile.TarInfo(name="index.html")
        info.size = len(data)
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _purge(uid: str) -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == uid)
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
        if job_ids:
            await s.execute(delete(LlmUsage).where(LlmUsage.job_id.in_(job_ids)))
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            await s.execute(delete(Answer).where(Answer.job_id.in_(job_ids)))
            await s.execute(delete(Question).where(Question.job_id.in_(job_ids)))
            await s.execute(delete(Revision).where(Revision.created_from_job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


@pytest_asyncio.fixture
async def job_in_state(autonomous_db):  # noqa: ANN001, ANN201
    """Фабрика: создаёт committed job в заданном state. Возвращает (make, storage)."""
    created: dict = {}
    storage = _FakeStorage()

    async def _make(state: JobState) -> tuple[str, str]:
        pid = new_project_id()
        jid = new_job_id()
        async with session_scope() as s:
            if not created:
                s.add(
                    User(
                        id=UID,
                        api_key_hash=hash_api_key("d2f-key"),
                        monthly_budget_usd=Decimal("50.0000"),
                        status="active",
                    )
                )
                created["user"] = True
            s.add(Project(id=pid, user_id=UID, prompt="Landing", title=None))
            s.add(
                GenerationJob(
                    id=jid,
                    project_id=pid,
                    user_id=UID,
                    state=state,
                    kind="generation",
                    budget_usd=Decimal("5.0000"),
                    spend_usd=Decimal("0.0000"),
                )
            )
            await s.commit()
        return pid, jid

    await _purge(UID)
    yield _make, storage
    await _purge(UID)


def _wire_deploy(monkeypatch, storage):  # noqa: ANN001, ANN202
    import app.pipeline.events as events
    import app.storage.s3 as s3mod
    import app.workers.tasks as tasks

    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop_publish)
    monkeypatch.setattr(tasks.docker_deploy, "publish_dist", lambda settings, project_id, d: d)
    return tasks


# --- build-fail → FIXING + failure_log с шапкой (§F) ---


async def test_build_fail_enters_fixing_writes_failure_log(job_in_state, monkeypatch):
    make, storage = job_in_state
    pid, jid = await make(JobState.BUILDING)
    tasks = _wire_deploy(monkeypatch, storage)

    # source.tgz по ключу джобы.
    import io
    import json
    import tarfile

    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {"index.html": b"<html></html>", "package.json": pkg.encode()}.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    storage.objects[s3.source_key(jid)] = buf.getvalue()

    # sandbox.run_build → неуспех (build-fail).
    def _fail_build(settings, ws, command, output_dir):  # noqa: ANN001, ANN202
        return tasks.sandbox.BuildResult(
            success=False, dist_dir=None, log="vite build failed\nerror: bad config\n"
        )

    monkeypatch.setattr(tasks.sandbox, "run_build", _fail_build)
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)
    monkeypatch.setattr(tasks.workspace, "safe_extract_tgz", lambda data, ws: None)

    await tasks._build_request(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING
        assert job.failure_log_ref == s3.build_log_key(jid)
        assert job.failure_event_pending is True
    # failure_log в правильном ключе с машинной шапкой (§F).
    log = storage.objects[s3.build_log_key(jid)].decode()
    parsed = parse_failure_log(log)
    assert parsed.failure_class == "build_error"


async def test_npm_install_error_classified(job_in_state, monkeypatch):
    make, storage = job_in_state
    pid, jid = await make(JobState.BUILDING)
    tasks = _wire_deploy(monkeypatch, storage)

    import io
    import json
    import tarfile

    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {"index.html": b"<html></html>", "package.json": pkg.encode()}.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    storage.objects[s3.source_key(jid)] = buf.getvalue()

    def _fail_npm(settings, ws, command, output_dir):  # noqa: ANN001, ANN202
        return tasks.sandbox.BuildResult(
            success=False, dist_dir=None, log="npm ERR! code ENOENT\nnpm ERR! missing\n"
        )

    monkeypatch.setattr(tasks.sandbox, "run_build", _fail_npm)
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)
    monkeypatch.setattr(tasks.workspace, "safe_extract_tgz", lambda data, ws: None)

    await tasks._build_request(jid)

    log = storage.objects[s3.build_log_key(jid)].decode()
    assert parse_failure_log(log).failure_class == "npm_install_error"


# --- deploy-fail → FIXING(deploy_error), teardown ДО FIXING ---


async def test_deploy_fail_classified_deploy_error_not_health(job_in_state, monkeypatch):
    make, storage = job_in_state
    pid, jid = await make(JobState.DEPLOYING)
    tasks = _wire_deploy(monkeypatch, storage)
    storage.objects[s3.dist_key(jid)] = _dist_tgz()

    order: list[str] = []

    def _fail_run(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        raise RuntimeError("docker run failed: boom")

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fail_run)
    monkeypatch.setattr(
        tasks.docker_deploy, "teardown_container", lambda cn: order.append("teardown")
    )
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)

    orig_enter = tasks.enter_fixing

    async def _spy_enter(*a, **k):  # noqa: ANN002, ANN003, ANN202
        order.append("enter_fixing")
        return await orig_enter(*a, **k)

    monkeypatch.setattr(tasks, "enter_fixing", _spy_enter)

    await tasks._deploy(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING
    log = storage.objects[s3.build_log_key(jid)].decode()
    assert parse_failure_log(log).failure_class == "deploy_error"  # НЕ health_timeout
    assert order == ["teardown", "enter_fixing"]  # teardown ДО FIXING


# --- health-fail → FIXING с health-классификацией ---


async def _run_health_fail(job_in_state, monkeypatch, detail: str):  # noqa: ANN001, ANN202
    make, storage = job_in_state
    pid, jid = await make(JobState.DEPLOYING)
    tasks = _wire_deploy(monkeypatch, storage)
    storage.objects[s3.dist_key(jid)] = _dist_tgz()

    def _ok_run(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return tasks.docker_deploy.DeployResult(
            container_id="cid", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _ok_run)
    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", lambda cn: None)
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)

    async def _fail_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=False, detail=detail)

    monkeypatch.setattr(tasks.health, "wait_until_live", _fail_health)

    await tasks._deploy(jid)
    log = storage.objects[s3.build_log_key(jid)].decode()
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        state = job.state
    return parse_failure_log(log).failure_class, state


async def test_health_timeout_classified(job_in_state, monkeypatch):
    cls, state = await _run_health_fail(job_in_state, monkeypatch, "timeout")
    assert cls == "health_timeout"
    assert state == JobState.FIXING


async def test_health_5xx_classified(job_in_state, monkeypatch):
    cls, _ = await _run_health_fail(job_in_state, monkeypatch, "last: status 502")
    assert cls == "health_5xx"


async def test_health_4xx_classified(job_in_state, monkeypatch):
    cls, _ = await _run_health_fail(job_in_state, monkeypatch, "last: status 404")
    assert cls == "health_4xx"
