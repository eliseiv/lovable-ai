"""Integration: FIXING happy-recovery (docs §B, 06-testing-strategy Sprint 2).

Полный восстановительный цикл одной джобы:
  BUILDING (1-я сборка fail) → FIXING → task_fix (Agent 4 валидный патч) → BUILDING
  (пересборка ok) → DEPLOYING → LIVE.

Реальный Postgres + Redis. Внешние границы — Claude (Agent 4 фейк), Docker
(publish_dist/run_nginx/teardown), health, sandbox.run_build, S3 (FakeStorage) — моки.
Каждая стадия прогоняется через настоящие task-функции (_build_request/_fix/_deploy);
переходы диспетчеризуются вручную (dispatch_for_state замокан спаем — в реальности
их ставит Celery).

Проверяет ключевой инвариант §B: retry_count инкрементирован РОВНО один раз (на
применённом патче FIXING→BUILDING), джоба доходит до LIVE.
"""

from __future__ import annotations

import io
import json
import tarfile
from decimal import Decimal
from pathlib import Path

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
from app.pipeline.agents.claude_client import AgentCall
from app.storage import s3

pytestmark = pytest.mark.asyncio

UID = "u_recoveryowner0000000"


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


def _src_tgz() -> bytes:
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
    return buf.getvalue()


def _valid_patch_json() -> str:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    return json.dumps(
        {
            "files": [
                {"path": "index.html", "encoding": "utf8", "content": "<html>fixed</html>"},
                {"path": "package.json", "encoding": "utf8", "content": pkg},
            ],
            "entry": "index.html",
            "build": {"tool": "vite", "command": "npm ci && vite build", "output_dir": "dist"},
        }
    )


def _call(text: str) -> AgentCall:
    return AgentCall(
        text=text,
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.0010"),
    )


class _FakeClient:
    """Фейк ClaudeAgentClient: ТЕКСТОВЫЙ режим (ADR-020 §I.1 revised) — структура из block.text."""

    _texts: list[str] = []

    def __init__(self, settings) -> None:  # noqa: ANN001
        pass

    async def run_agent(  # noqa: ANN201
        self,
        *,
        agent,
        model,
        system_prompt,
        user_content,  # noqa: ANN001
    ):
        text = type(self)._texts.pop(0) if type(self)._texts else "{}"
        return _call(text)


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
async def building_job(autonomous_db):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    await _purge(UID)
    storage = _FakeStorage()
    storage.objects[s3.source_key(jid)] = _src_tgz()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("rec-key"),
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
                state=JobState.BUILDING,
                kind="generation",
                spec_tz="# Spec",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
                max_fix_attempts=3,
                retry_count=0,
            )
        )
        await s.commit()
    yield pid, jid, storage
    await _purge(UID)


async def test_full_recovery_building_fix_building_deploying_live(
    building_job, monkeypatch, tmp_path
):
    pid, jid, storage = building_job

    import app.pipeline.agents.agent4 as agent4_mod
    import app.storage.s3 as s3mod
    import app.workers.tasks as tasks

    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)
    monkeypatch.setattr(agent4_mod, "ClaudeAgentClient", _FakeClient)
    _FakeClient._texts = [_valid_patch_json()]

    # dispatch_for_state — спай (в реальности ставит Celery; здесь гоняем стадии вручную).
    dispatched: list = []
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda j, st: dispatched.append((j, st)))

    # --- build: 1-я сборка fail → DEPLOYING→FIXING (через enter_fixing) ---
    build_calls = {"n": 0}

    def _build(settings, ws, command, output_dir):  # noqa: ANN001, ANN202
        build_calls["n"] += 1
        if build_calls["n"] == 1:
            # Первая сборка — fail (доменный build-fail → FIXING).
            return tasks.sandbox.BuildResult(success=False, dist_dir=None, log="error: boom\n")
        # Вторая (после патча) — успех: материализуем dist/.
        dist = tmp_path / f"dist_{build_calls['n']}"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html>fixed</html>")
        return tasks.sandbox.BuildResult(success=True, dist_dir=dist, log="ok\n")

    monkeypatch.setattr(tasks.sandbox, "run_build", _build)
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)
    monkeypatch.setattr(
        tasks.workspace, "safe_extract_tgz", lambda data, dest: _extract(data, dest)
    )

    await tasks._build_request(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING

    # --- fix: Agent 4 валидный патч → FIXING→BUILDING, retry_count++ ---
    await tasks._fix(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.BUILDING
        assert job.retry_count == 1

    # --- build (повтор): успех → BUILDING→DEPLOYING ---
    await tasks._build_request(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.DEPLOYING

    # --- deploy: docker+health ok → DEPLOYING→LIVE ---
    monkeypatch.setattr(tasks.docker_deploy, "publish_dist", lambda settings, project_id, d: d)

    def _run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return tasks.docker_deploy.DeployResult(
            container_id="cid_live", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _run_nginx)
    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", lambda cn: None)

    async def _health_ok(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _health_ok)

    await tasks._deploy(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.LIVE
        # retry_count инкрементирован РОВНО один раз за весь цикл (§B п.3).
        assert job.retry_count == 1
        proj = await s.get(Project, pid)
        assert proj.current_revision_id is not None
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "active"


def _extract(data: bytes, dest: Path) -> None:
    """Минимальная безопасная распаковка для теста (regular files)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name == ".build.json":
                continue
            if member.isreg():
                tar.extract(member, path=dest, set_attrs=False)
