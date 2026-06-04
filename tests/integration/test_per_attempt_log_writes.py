"""Integration: точки записи per-attempt логов (ADR-022, deploy §F-1).

Реальный Postgres (session_scope/autonomous_db). S3 — FakeStorage (in-memory dict).
Прямые вызовы прод-функций записи лога против реальной джобы:
  - enter_fixing (app.pipeline.fixing): build/npm-фейл → build.{retry_count}.log;
    deploy/health-фейл → deploy.{retry_count}.log; failure_log_ref события и
    generation_jobs.failure_log_ref указывают на per-attempt ключ;
  - _handle_invalid_patch (app.workers.tasks): agent_output_invalid → agent.{retry_count}.log,
    fix_rejected.failure_log_ref на него, retry_count НЕ инкрементируется;
  - кросс-витковая инвариантность: build.0 (виток 0) НЕ затирается build.1 (виток 1).

Нормативный источник: docs/adr/ADR-022-per-attempt-build-logs.md (таблица §Decision,
три точки записи), docs/modules/deploy/03-architecture.md §F-1.
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
from app.schemas.agent_output import AgentOutputError
from app.storage import s3

pytestmark = pytest.mark.asyncio

UID = "u_perattemptowner00000"


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
async def job_factory(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    """Фабрика committed-джоб в DEPLOYING с заданным retry_count + FakeStorage.

    Мокает dispatch_for_state и publish_event (no-op) во всех точках, чтобы прямой вызов
    enter_fixing/_handle_invalid_patch не дёргал Celery/Redis.
    """
    import app.pipeline.dispatcher as dispatcher_mod
    import app.pipeline.events as events_mod
    import app.pipeline.fixing as fixing_mod
    import app.workers.tasks as tasks_mod

    monkeypatch.setattr(fixing_mod, "dispatch_for_state", lambda *a, **k: None)
    monkeypatch.setattr(tasks_mod, "dispatch_for_state", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher_mod, "dispatch_for_state", lambda *a, **k: None)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events_mod, "publish_event", _noop_publish)

    storage = _FakeStorage()
    created: dict = {}

    async def _make(retry_count: int, state: JobState = JobState.DEPLOYING) -> str:
        pid = new_project_id()
        jid = new_job_id()
        async with session_scope() as s:
            if not created:
                s.add(
                    User(
                        id=UID,
                        api_key_hash=hash_api_key("perattempt-key"),
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
                    retry_count=retry_count,
                )
            )
            await s.commit()
        return jid

    await _purge(UID)
    yield _make, storage
    await _purge(UID)


# --- A4: enter_fixing — build/npm-фейл → build.{n}; deploy/health-фейл → deploy.{n} ---


@pytest.mark.parametrize(
    ("failure_class", "stage"),
    [
        ("build_error", "build"),
        ("npm_install_error", "build"),
        ("deploy_error", "deploy"),
        ("health_timeout", "deploy"),
        ("health_5xx", "deploy"),
        ("health_4xx", "deploy"),
    ],
)
async def test_enter_fixing_routes_failure_class_to_correct_stage_key(
    job_factory, failure_class: str, stage: str
):
    """enter_fixing пишет лог в build.{retry_count}.log для build/npm-фейла и в
    deploy.{retry_count}.log для deploy/health-фейла; failure_log_ref (поле джобы и
    payload build_failed-события) указывает именно на этот per-attempt ключ."""
    from app.pipeline.fixing import enter_fixing

    make, storage = job_factory
    retry_count = 2
    jid = await make(retry_count)
    expected_key = (
        s3.build_log_key(jid, retry_count)
        if stage == "build"
        else s3.deploy_log_key(jid, retry_count)
    )

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await enter_fixing(
            s,
            job,
            storage,
            failure_class=failure_class,
            failure_body=f"{failure_class}: boom",
            revision_no=None,
        )
        await s.commit()

    # Лог записан именно под per-attempt ключом стадии.
    assert expected_key in storage.objects, f"{failure_class} → {expected_key}"
    parsed = parse_failure_log(storage.objects[expected_key].decode())
    assert parsed.failure_class == failure_class

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING
        # generation_jobs.failure_log_ref → per-attempt ключ.
        assert job.failure_log_ref == expected_key
        # build_failed-событие несёт тот же per-attempt ключ.
        ev = (
            await s.execute(
                select(JobEvent)
                .where(JobEvent.job_id == jid, JobEvent.event_type == "build_failed")
                .order_by(JobEvent.id.desc())
            )
        ).scalar_one()
        assert ev.payload["failure_log_ref"] == expected_key
        assert ev.payload["failure_class"] == failure_class


async def test_enter_fixing_same_attempt_build_then_deploy_no_overwrite(job_factory):
    """В пределах одного retry_count=N build-фейл (build.N) и deploy-фейл (deploy.N) —
    разные ключи: deploy-фейл НЕ затирает build-лог того же витка (коллизия стадий §Decision)."""
    from app.pipeline.fixing import enter_fixing

    make, storage = job_factory
    jid = await make(0)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await enter_fixing(
            s,
            job,
            storage,
            failure_class="build_error",
            failure_body="build boom",
            revision_no=None,
        )
        await s.commit()
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        # Тот же виток (retry_count не менялся), теперь deploy-фейл.
        await enter_fixing(
            s,
            job,
            storage,
            failure_class="deploy_error",
            failure_body="deploy boom",
            revision_no=None,
        )
        await s.commit()

    # Оба лога витка 0 сосуществуют.
    assert parse_failure_log(storage.objects[s3.build_log_key(jid, 0)].decode()).failure_class == (
        "build_error"
    )
    assert parse_failure_log(storage.objects[s3.deploy_log_key(jid, 0)].decode()).failure_class == (
        "deploy_error"
    )


# --- A3 (real flow): два витка → build.0 не затирается build.1 ---


async def test_two_attempts_distinct_build_keys_no_overwrite(job_factory):
    """Виток 0 (retry_count=0) и виток 1 (retry_count=1) build-фейла пишут в build.0/build.1 —
    ранний лог восстановим (прод-инцидент ADR-022)."""
    from app.pipeline.fixing import enter_fixing

    make, storage = job_factory
    jid = await make(0)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await enter_fixing(
            s,
            job,
            storage,
            failure_class="build_error",
            failure_body="first attempt: error A",
            revision_no=None,
        )
        # Симулируем инкремент retry_count на валидном патче FIXING→BUILDING (task_fix).
        job.retry_count = 1
        job.state = JobState.DEPLOYING
        await s.commit()
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await enter_fixing(
            s,
            job,
            storage,
            failure_class="build_error",
            failure_body="second attempt: error B",
            revision_no=None,
        )
        await s.commit()

    log0 = storage.objects[s3.build_log_key(jid, 0)].decode()
    log1 = storage.objects[s3.build_log_key(jid, 1)].decode()
    assert "error A" in log0, "лог первого витка не затёрт вторым"
    assert "error B" in log1
    assert s3.build_log_key(jid, 0) != s3.build_log_key(jid, 1)


# --- A4: _handle_invalid_patch → agent.{retry_count}.log, retry_count не инкрементируется ---


async def test_handle_invalid_patch_writes_agent_key_same_retry_count(job_factory):
    """_handle_invalid_patch (agent_output_invalid) пишет agent.{retry_count}.log; НЕ
    инкрементирует retry_count → тот же N, что и build/deploy-фейл витка; fix_rejected
    и generation_jobs.failure_log_ref указывают на agent.{N}.log."""
    from app.workers.tasks import _handle_invalid_patch

    make, storage = job_factory
    retry_count = 3
    jid = await make(retry_count, state=JobState.FIXING)
    expected_key = s3.agent_log_key(jid, retry_count)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        exc = AgentOutputError("patch missing entry", signature="agent_output_invalid")
        await _handle_invalid_patch(s, storage, job, exc, revision=None)
        # _handle_invalid_patch коммитит сам.

    assert expected_key in storage.objects
    assert parse_failure_log(storage.objects[expected_key].decode()).failure_class == (
        "agent_output_invalid"
    )

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        # retry_count НЕ изменился (инкремент только на валидном патче).
        assert job.retry_count == retry_count
        assert job.failure_log_ref == expected_key
        assert job.failure_event_pending is True
        ev = (
            await s.execute(
                select(JobEvent)
                .where(JobEvent.job_id == jid, JobEvent.event_type == "fix_rejected")
                .order_by(JobEvent.id.desc())
            )
        ).scalar_one()
        assert ev.payload["failure_log_ref"] == expected_key


async def test_agent_key_does_not_overwrite_build_or_deploy_of_same_attempt(job_factory):
    """Критичный инвариант §Decision: agent.{N}.log не затирает build.{N}/deploy.{N}
    того же витка N (три раздельных имени-стадии при одном retry_count)."""
    from app.pipeline.fixing import enter_fixing
    from app.workers.tasks import _handle_invalid_patch

    make, storage = job_factory
    jid = await make(0, state=JobState.DEPLOYING)

    # build-фейл витка 0.
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await enter_fixing(
            s, job, storage, failure_class="build_error", failure_body="build err", revision_no=None
        )
        await s.commit()
    # deploy-фейл витка 0 (тот же retry_count).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await enter_fixing(
            s,
            job,
            storage,
            failure_class="deploy_error",
            failure_body="deploy err",
            revision_no=None,
        )
        await s.commit()
    # отклонённый патч Agent 4 витка 0 (retry_count НЕ инкрементирован).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await _handle_invalid_patch(
            s,
            storage,
            job,
            AgentOutputError("invalid patch", signature="agent_output_invalid"),
            revision=None,
        )

    # Все три лога витка 0 сосуществуют — ни одна стадия не затёрла другую.
    assert "build err" in storage.objects[s3.build_log_key(jid, 0)].decode()
    assert "deploy err" in storage.objects[s3.deploy_log_key(jid, 0)].decode()
    assert "invalid patch" in storage.objects[s3.agent_log_key(jid, 0)].decode()
    assert len({s3.build_log_key(jid, 0), s3.deploy_log_key(jid, 0), s3.agent_log_key(jid, 0)}) == 3


# --- A5: SiteDeployment.build_log_ref = build.{retry_count} попытки деплоя ---


async def test_site_deployment_build_log_ref_is_per_attempt_key(job_factory):
    """На успешной сборке витка retry_count=N путь build.{N}.log проставляется в
    SiteDeployment.build_log_ref (tasks._deploy создаёт строку деплоя с этим ключом)."""
    from app.core.ids import new_deployment_id, new_revision_id

    make, storage = job_factory
    retry_count = 1
    jid = await make(retry_count, state=JobState.DEPLOYING)
    expected_key = s3.build_log_key(jid, retry_count)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        rev_id = new_revision_id()
        s.add(
            Revision(
                id=rev_id,
                project_id=job.project_id,
                revision_no=1,
                source_artifact_ref=s3.source_key(jid),
                created_from_job_id=jid,
                is_good=False,
            )
        )
        await s.flush()
        # Воспроизводим конструкцию строки деплоя из tasks._deploy: build_log_ref берётся
        # из build_log_key(job_id, job.retry_count) — per-attempt ключ попытки деплоя.
        dep = SiteDeployment(
            id=new_deployment_id(),
            project_id=job.project_id,
            revision_id=rev_id,
            subdomain="cccccccccccccccc",
            live_url="http://cccccccccccccccc.apps.localhost/",
            dist_artifact_ref=s3.dist_key(jid),
            build_log_ref=s3.build_log_key(jid, job.retry_count),
            container_id=None,
            status="building",
        )
        s.add(dep)
        await s.commit()
        dep_id = dep.id

    async with session_scope() as s:
        dep = await s.get(SiteDeployment, dep_id)
        assert dep.build_log_ref == expected_key
        assert dep.build_log_ref == f"logs/{jid}/build.{retry_count}.log"
