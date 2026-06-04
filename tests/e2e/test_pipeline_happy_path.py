"""E2E happy-path (главный DoD Sprint 1, docs/06-testing-strategy.md).

Прогоняет ВЕСЬ pipeline через тела Celery-тасок (_interview→_spec→_build_request→
_deploy), state-machine CREATED→...→LIVE с реальным live_url. Внешние границы
(Claude / S3 / vite-sandbox / docker-deploy / health) замоканы для детерминизма;
Postgres — реальный. Это автоматизируемая замена «промт→LIVE» без внешних сервисов.

Реальный E2E на поднятом стеке (ANTHROPIC_API_KEY + Docker + WSL2) — см.
test_real_stack_e2e.py (skip без окружения).
"""

from __future__ import annotations

import json
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
    UsageCounter,
    User,
)
from app.db.session import session_scope
from app.pipeline.agents.agent1 import Agent1Result, ParsedQuestion
from app.pipeline.agents.agent2 import Agent2Result
from app.pipeline.agents.agent3 import Agent3Result
from app.pipeline.agents.claude_client import AgentCall
from app.schemas.agent_output import ValidatedFile, ValidatedTree

pytestmark = pytest.mark.asyncio

UID = "u_e2eowner00000000000000"


def _call(model="claude-opus-4-8", cost="0.0500"):  # noqa: ANN001, ANN201
    return AgentCall(
        text="raw",
        model=model,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=5,
        cost_usd=Decimal(cost),
    )


def _validated_tree():  # noqa: ANN201
    pkg = json.dumps(
        {"name": "site", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    ).encode()
    return ValidatedTree(
        files=(
            ValidatedFile("package.json", "utf8", pkg),
            ValidatedFile("index.html", "utf8", b"<html><body>hi</body></html>"),
            ValidatedFile("src/main.js", "utf8", b"console.log(1)"),
        ),
        entry="index.html",
        build_command="npm ci && vite build",
        build_output_dir="dist",
    )


class _FakeStorage:
    """In-memory S3: put/get по ключу (детерминированно)."""

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


@pytest_asyncio.fixture
async def e2e_project(autonomous_db):  # noqa: ANN001, ANN201
    """Committed user+project+job (CREATED) для прогона pipeline; чистит в teardown."""
    pid = new_project_id()
    jid = new_job_id()
    await _purge(UID)
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("e2e-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="Landing page for a coffee shop", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=JobState.CREATED,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
            )
        )
        await s.commit()
    yield pid, jid
    await _purge(UID)


async def _purge(uid: str) -> None:
    """Идемпотентная FK-safe очистка всех данных пользователя."""
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
        # Снять ссылку projects.current_revision_id перед удалением revisions/projects.
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
        # Sprint 3.5: pipeline-старт инкрементит usage_counters → FK на users; чистим перед user.
        await s.execute(delete(UsageCounter).where(UsageCounter.user_id == uid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


async def test_full_pipeline_created_to_live(e2e_project, monkeypatch):
    pid, jid = e2e_project
    import app.deploy.docker_deploy as docker_deploy
    import app.deploy.health as health_mod
    import app.deploy.sandbox as sandbox
    import app.pipeline.events as events
    import app.storage.s3 as s3mod
    import app.workers.tasks as tasks

    storage = _FakeStorage()

    # --- mock внешних границ ---
    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop_publish)

    # dispatch — no-op: мы вручную ведём состояния, чтобы прогон был детерминирован.
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)

    # Agent 1/2/3 — фейки (без Claude). ADR-020 §I: обёртки агентов теперь принимают
    # инъектируемые хуки (before_call/after_call/on_attempt_failure) от task-слоя. usage
    # пишется хуком after_call ПОСЛЕ вызова (а не task'ом из result.call) — фейк вызывает
    # before_call (budget/wall-clock-гард) + after_call(call) (cost-ledger), как реальный агент.
    async def _fake_agent1(settings, prompt, *, before_call, after_call, on_attempt_failure):  # noqa: ANN001, ANN202
        await before_call()
        call = _call()
        await after_call(call)
        return Agent1Result(
            questions=[
                ParsedQuestion(position=1, text="Brand colors?", kind="free_text", options=None),
                ParsedQuestion(position=2, text="Sections?", kind="free_text", options=None),
            ],
            call=call,
        )

    async def _fake_agent2(
        settings, prompt, qa_pairs, *, before_call, after_call, on_attempt_failure
    ):  # noqa: ANN001, ANN202, E501
        await before_call()
        call = _call()
        await after_call(call)
        return Agent2Result(spec_markdown="# Spec\nCoffee shop landing.", call=call)

    async def _fake_agent3(settings, spec, *, before_call, after_call, on_attempt_failure):  # noqa: ANN001, ANN202
        await before_call()
        call = _call(model="claude-sonnet-4-6")
        await after_call(call)
        return Agent3Result(tree=_validated_tree(), call=call)

    monkeypatch.setattr(tasks, "run_agent1", _fake_agent1)
    monkeypatch.setattr(tasks, "run_agent2", _fake_agent2)
    monkeypatch.setattr(tasks, "run_agent3", _fake_agent3)

    # sandbox.run_build → успех, реально материализует dist/ из workspace.
    def _fake_run_build(settings, ws, command, output_dir):  # noqa: ANN001, ANN202
        dist = ws / output_dir
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_bytes(b"<html><body>hi</body></html>")
        return sandbox.BuildResult(success=True, log="build ok", dist_dir=dist)

    monkeypatch.setattr(tasks.sandbox, "run_build", _fake_run_build)

    # docker_deploy → фейковый контейнер.
    def _fake_publish_dist(settings, project_id, dist_dir):  # noqa: ANN001, ANN202
        return dist_dir

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return docker_deploy.DeployResult(
            container_id="cid_fake", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "publish_dist", _fake_publish_dist)
    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    # health → live сразу.
    async def _fake_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return health_mod.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health)

    # --- прогон стадий pipeline ---
    # 1. INTERVIEW: CREATED → AWAITING_CLARIFICATION (+вопросы).
    await tasks._interview(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.AWAITING_CLARIFICATION
        qs = (await s.execute(select(Question).where(Question.job_id == jid))).scalars().all()
        assert len(qs) == 2
        qids = [q.id for q in qs]

    # 2. ANSWERS: резюм AWAITING_CLARIFICATION → SPECCING.
    from app.schemas.api import AnswerItem
    from app.services.answers_service import AnswersOutcome, submit_answers

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        res = await submit_answers(
            s,
            job=job,
            items=[
                AnswerItem(question_id=qids[0], text="blue"),
                AnswerItem(question_id=qids[1], text="hero, menu"),
            ],
        )
        assert res.outcome == AnswersOutcome.APPLIED
    async with session_scope() as s:
        assert (await s.get(GenerationJob, jid)).state == JobState.SPECCING

    # 3. SPEC: SPECCING → BUILDING (Agent2+Agent3, source.tgz упакован).
    await tasks._spec(jid)
    async with session_scope() as s:
        assert (await s.get(GenerationJob, jid)).state == JobState.BUILDING

    # 4. BUILD: BUILDING → DEPLOYING (vite build, dist.tgz).
    await tasks._build_request(jid)
    async with session_scope() as s:
        assert (await s.get(GenerationJob, jid)).state == JobState.DEPLOYING

    # 5. DEPLOY: DEPLOYING → LIVE (nginx+Traefik+health).
    await tasks._deploy(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.LIVE, f"итоговое состояние {job.state}"
        # Реальный live_url создан и активен.
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "active"
        assert dep.live_url.startswith("http://")
        assert dep.live_url.endswith(".apps.localhost/")
        # current_revision_id проставлен.
        proj = await s.get(Project, pid)
        assert proj.current_revision_id is not None
        # Cost-ledger: usage по всем трём агентам.
        usage = (await s.execute(select(LlmUsage).where(LlmUsage.job_id == jid))).scalars().all()
        agents = {u.agent for u in usage}
        assert {"agent1", "agent2", "agent3"} <= agents
        assert job.spend_usd > Decimal("0")
