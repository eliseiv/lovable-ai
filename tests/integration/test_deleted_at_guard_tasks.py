"""Integration: deleted_at-guard в продвигающих тасках (ADR-011 §C, follow_up_for_qa #2/#3).

Гонка GC↔in-flight виток (ADR-011 §C / deploy §6 шаг 1): проект мог быть soft-deleted
(projects.deleted_at) между постановкой Celery-таски и её исполнением. Helper
_abort_if_project_deleted в КАЖДОЙ продвигающей таске (_interview/_spec/_build_request/
_fix/_deploy) обязан это поймать ПЕРЕД дорогой работой (вызов Claude / docker run) и
перевести джобу в терминальный FAILED(project_deleted) БЕЗ вызова агентов.

Покрытие (реальный Postgres + Redis; Claude/Docker/health/S3 — моки):
  #2 soft-delete во время CREATED/SPECCING/BUILDING/FIXING → FAILED(project_deleted)
     БЕЗ вызова Agent 1/2/3/4 (проверяется моками run_agentN: НЕ вызваны);
     happy-path (deleted_at IS NULL) НЕ ломается (агенты дёргаются, джоба продвигается);
  #3 TOCTOU-гонка _deploy↔project.gc: deleted_at выставлен МЕЖДУ run_nginx_container и
     финалом _deploy (после докер-run, перед LIVE) → teardown_container вызван,
     deployment.status=failed, job FAILED(project_deleted), НЕ LIVE, orphan-контейнера/
     Traefik-route не остаётся.

Изоляция autonomous_db: таски используют session_scope (раздельные транзакции), поэтому
данные коммитятся в реальную БД и подчищаются _purge в teardown фикстур.

real-stack энфорс (реальный docker rm -f сносит контейнер/route) — живой пункт S4.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    UsageCounter,
    User,
)
from app.db.session import session_scope
from app.storage import s3

pytestmark = pytest.mark.asyncio

_UID = "u_delguard00000000000000"


async def _purge() -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == _UID)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == _UID)
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
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == _UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(UsageCounter).where(UsageCounter.user_id == _UID))
        await s.execute(delete(User).where(User.id == _UID))
        await s.commit()


async def _seed_job(state: JobState, *, deleted: bool) -> tuple[str, str]:
    """Создаёт committed user+project(±soft-delete)+job в state. Возвращает (pid, jid)."""
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(
            User(
                id=_UID,
                api_key_hash=hash_api_key("delguard-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(
            Project(
                id=pid,
                user_id=_UID,
                prompt="Landing page about cats",
                title=None,
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=_UID,
                state=state,
                kind="generation",
                spec_tz="# Spec",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
                max_fix_attempts=3,
                retry_count=0,
            )
        )
        await s.commit()
    return pid, jid


@pytest_asyncio.fixture(autouse=True)
async def _clean(autonomous_db):  # noqa: ANN001, ANN202
    await _purge()
    yield
    await _purge()


@pytest.fixture
def _noop_publish(monkeypatch):  # noqa: ANN001, ANN202
    import app.pipeline.events as events

    async def _noop(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop)
    return _noop


def _guard_agents(monkeypatch) -> dict[str, int]:
    """Мокает run_agent1..4 ассерт-страйкерами: любой вызов = провал (Claude не должен дёргаться).

    Возвращает счётчик (остаётся {0,0,0,0} в guard-кейсе). Для happy-path тесты подменяют
    конкретный агент стабом отдельно.
    """
    import app.workers.tasks as tasks

    calls = {"a1": 0, "a2": 0, "a3": 0, "a4": 0}

    async def _a1(*a, **k):  # noqa: ANN002, ANN003, ANN202
        calls["a1"] += 1
        raise AssertionError("Agent 1 (Claude) не должен вызываться на soft-deleted проекте")

    async def _a2(*a, **k):  # noqa: ANN002, ANN003, ANN202
        calls["a2"] += 1
        raise AssertionError("Agent 2 (Claude) не должен вызываться на soft-deleted проекте")

    async def _a3(*a, **k):  # noqa: ANN002, ANN003, ANN202
        calls["a3"] += 1
        raise AssertionError("Agent 3 (Claude) не должен вызываться на soft-deleted проекте")

    async def _a4(*a, **k):  # noqa: ANN002, ANN003, ANN202
        calls["a4"] += 1
        raise AssertionError("Agent 4 (Claude) не должен вызываться на soft-deleted проекте")

    monkeypatch.setattr(tasks, "run_agent1", _a1)
    monkeypatch.setattr(tasks, "run_agent2", _a2)
    monkeypatch.setattr(tasks, "run_agent3", _a3)
    monkeypatch.setattr(tasks, "run_agent4", _a4)
    return calls


async def _assert_failed_project_deleted(jid: str) -> None:
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED, job.state
        assert job.failure_reason == "project_deleted", job.failure_reason


# --- #2: soft-delete во время CREATED/SPECCING/BUILDING/FIXING → FAILED, без Claude ---


async def test_interview_aborts_on_softdeleted_no_agent1(monkeypatch, _noop_publish):
    """CREATED soft-deleted → _interview → FAILED(project_deleted), Agent 1 НЕ вызван."""
    import app.workers.tasks as tasks

    calls = _guard_agents(monkeypatch)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    _, jid = await _seed_job(JobState.CREATED, deleted=True)

    await tasks._interview(jid)

    await _assert_failed_project_deleted(jid)
    assert calls["a1"] == 0


async def test_spec_aborts_on_softdeleted_no_agent2_3(monkeypatch, _noop_publish):
    """SPECCING soft-deleted → _spec → FAILED(project_deleted), Agent 2/3 НЕ вызваны."""
    import app.workers.tasks as tasks

    calls = _guard_agents(monkeypatch)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    _, jid = await _seed_job(JobState.SPECCING, deleted=True)

    await tasks._spec(jid)

    await _assert_failed_project_deleted(jid)
    assert calls["a2"] == 0
    assert calls["a3"] == 0


async def test_build_request_aborts_on_softdeleted_no_sandbox(monkeypatch, _noop_publish):
    """BUILDING soft-deleted → _build_request → FAILED(project_deleted), сборка НЕ запущена."""
    import app.workers.tasks as tasks

    _guard_agents(monkeypatch)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)

    def _must_not_build(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("sandbox.run_build не должен вызываться на soft-deleted проекте")

    monkeypatch.setattr(tasks.sandbox, "run_build", _must_not_build)
    _, jid = await _seed_job(JobState.BUILDING, deleted=True)

    await tasks._build_request(jid)

    await _assert_failed_project_deleted(jid)


async def test_fix_aborts_on_softdeleted_no_agent4(monkeypatch, _noop_publish):
    """FIXING soft-deleted → _fix → FAILED(project_deleted), Agent 4 Fixer НЕ вызван."""
    import app.workers.tasks as tasks

    calls = _guard_agents(monkeypatch)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    _, jid = await _seed_job(JobState.FIXING, deleted=True)

    await tasks._fix(jid)

    await _assert_failed_project_deleted(jid)
    assert calls["a4"] == 0


# --- #2 happy-path: deleted_at IS NULL не ломается (агент дёргается, джоба продвигается) ---


async def test_interview_happy_path_not_broken_by_guard(monkeypatch, _noop_publish):
    """deleted_at IS NULL: _interview продвигает CREATED→AWAITING_CLARIFICATION, Agent 1 вызван.

    Контроль, что guard не ломает happy-path (false-positive abort). Agent 1 — стаб
    (возвращает один вопрос), без обращения к Claude.
    """
    import app.workers.tasks as tasks
    from app.pipeline.agents.agent1 import Agent1Result, ParsedQuestion
    from app.pipeline.agents.claude_client import AgentCall

    agent1_called = {"n": 0}

    async def _stub_agent1(settings, prompt):  # noqa: ANN001, ANN202
        agent1_called["n"] += 1
        call = AgentCall(
            text="{}",
            model="claude-sonnet-4",
            input_tokens=5,
            output_tokens=2,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=Decimal("0.0005"),
        )
        return Agent1Result(
            questions=[
                ParsedQuestion(position=1, text="What color?", kind="free_text", options=None)
            ],
            call=call,
        )

    monkeypatch.setattr(tasks, "run_agent1", _stub_agent1)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    _, jid = await _seed_job(JobState.CREATED, deleted=False)

    await tasks._interview(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.AWAITING_CLARIFICATION, job.state
        assert job.failure_reason is None
    assert agent1_called["n"] == 1, "happy-path: Agent 1 должен быть вызван (guard не ломает)"


# --- #3: TOCTOU-гонка _deploy↔project.gc (deleted_at между run_nginx и финалом) ---


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
        data = b"<html>site</html>"
        info = tarfile.TarInfo(name="index.html")
        info.size = len(data)
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_deploy_toctou_softdelete_after_run_nginx_teardown_no_live(
    monkeypatch, _noop_publish, tmp_path
):
    """TOCTOU (ADR-011 §C / deploy §6): deleted_at выставлен МЕЖДУ run_nginx_container и финалом.

    Первый guard (до docker run) пройден — проект ещё жив. project.gc soft-delete'ит проект
    во время длинного wait_until_live (после фактического docker run). TOCTOU-перепроверка
    deleted_at после health → teardown_container текущего контейнера, deployment.status=failed,
    job FAILED(project_deleted), НЕ LIVE, orphan-контейнера/Traefik-route не остаётся.

    Гонка смоделирована: health-mock внутри себя выставляет projects.deleted_at != None
    (в отдельной session_scope-транзакции — как реальный GC), затем возвращает ok=True,
    чтобы код дошёл до пост-run re-check (а не свернул через health-fail-ветку).
    """
    import app.workers.tasks as tasks

    storage = _FakeStorage()

    pid, jid = await _seed_job(JobState.DEPLOYING, deleted=False)
    # dist уже собран (build прошёл): кладём артефакт по ключу джобы.
    storage.objects[s3.dist_key(jid)] = _dist_tgz()

    import app.storage.s3 as s3mod

    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    _guard_agents(monkeypatch)  # ни один агент в _deploy не нужен

    # Docker/FS-границы — изолированы.
    monkeypatch.setattr(
        tasks.workspace,
        "safe_extract_tgz",
        lambda data, dest: Path(dest).mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)
    monkeypatch.setattr(tasks.docker_deploy, "publish_dist", lambda s, p, ws: tmp_path / "site")

    run_calls = {"n": 0}

    def _run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        run_calls["n"] += 1
        return tasks.docker_deploy.DeployResult(
            container_id="cid-1", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _run_nginx)

    teardown_calls: list[str] = []

    def _teardown(container_name):  # noqa: ANN001, ANN202
        teardown_calls.append(container_name)

    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", _teardown)

    # health-mock МОДЕЛИРУЕТ гонку: soft-delete проекта (как project.gc, отдельная транзакция)
    # ПОСЛЕ фактического docker run, затем ok=True — чтобы _deploy дошёл до TOCTOU re-check.
    async def _race_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        async with session_scope() as s:
            proj = await s.get(Project, pid)
            proj.deleted_at = datetime.now(UTC)
            await s.commit()
        return tasks.health.HealthResult(ok=True, detail="200 ok")

    monkeypatch.setattr(tasks.health, "wait_until_live", _race_health)

    await tasks._deploy(jid)

    # docker run произошёл (первый guard пройден — гонка именно TOCTOU, после run).
    assert run_calls["n"] == 1, "run_nginx_container должен был выполниться до гонки (TOCTOU)"
    # teardown текущего контейнера вызван ровно один раз (снять orphan + Traefik-route через rm).
    subdomain = await _subdomain_of(pid)
    assert teardown_calls == [f"site_{subdomain}"], (
        f"ожидается ровно один teardown текущего контейнера site_{subdomain}: {teardown_calls}"
    )

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED, "TOCTOU: джоба НЕ LIVE"
        assert job.state != JobState.LIVE
        assert job.failure_reason == "project_deleted", job.failure_reason
        deps = (
            (await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid)))
            .scalars()
            .all()
        )
        # Строка деплоя создана (building→failed), но статус failed, не active (не LIVE).
        assert len(deps) == 1, deps
        assert deps[0].status == "failed", deps[0].status


async def _subdomain_of(pid: str) -> str:
    async with session_scope() as s:
        dep = (
            (await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid)))
            .scalars()
            .first()
        )
        return dep.subdomain if dep is not None else ""
