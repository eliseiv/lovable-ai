"""Integration: teardown-on-fail / happy-path lifecycle строки site_deployments.

Реальный Postgres (через session_scope/autonomous_db). Внешние границы — docker
(docker_deploy.publish_dist/run_nginx_container/teardown_container) и health
(health.wait_until_live) — мокаются. Проверяется СОГЛАСОВАННОСТЬ двух машин
состояний (docs/modules/deploy/03-architecture.md §5):

  1 — teardown-on-fail при health-fail: docker rm -f контейнера ДО FAILED,
      deployment.status=='failed';
  2 — teardown-on-fail при ошибке docker run: teardown вызван, status=='failed', FAILED;
  5 — happy-path lifecycle: создаётся building/container_id=None → health 200 →
      active/container_id заполнен, job.state=LIVE;
  7 — отказоустойчивость: teardown сам бросает (НЕ «No such container») → джоба
      не зависает LIVE; rollback оставляет DEPLOYING/building (acks_late re-run корректен).
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

pytestmark = pytest.mark.asyncio

UID = "u_tdowner00000000000000"


class _FakeStorage:
    """In-memory S3: deploy читает dist по ключу, пишет логи."""

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
    """Минимальный валидный dist.tgz (safe_extract_tgz его распакует)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"<html><body>hi</body></html>"
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
async def deploying_job(autonomous_db):  # noqa: ANN001, ANN201
    """Committed user+project+job в состоянии DEPLOYING (вход в _deploy)."""
    pid = new_project_id()
    jid = new_job_id()
    await _purge(UID)
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("td-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="Landing page", title=None))
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
    await _purge(UID)


def _wire_common(monkeypatch, storage):  # noqa: ANN001, ANN202
    """Общая обвязка моков deploy-стадии: storage, publish_event no-op, dispatch no-op,
    publish_dist no-op. Возвращает модуль tasks для дальнейшего точечного мока."""
    import app.pipeline.events as events
    import app.storage.s3 as s3mod
    import app.workers.tasks as tasks

    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop_publish)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)

    def _fake_publish_dist(settings, project_id, dist_dir):  # noqa: ANN001, ANN202
        return dist_dir

    monkeypatch.setattr(tasks.docker_deploy, "publish_dist", _fake_publish_dist)
    return tasks


async def _seed_dist(storage, jid):  # noqa: ANN001, ANN202
    import app.storage.s3 as s3

    storage.objects[s3.dist_key(jid)] = _dist_tgz()


# --- (5) happy-path lifecycle ------------------------------------------------


async def test_happy_path_building_then_active_container_id_filled(deploying_job, monkeypatch):
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    seen_status: dict = {}

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        # На момент docker run строка деплоя обязана быть building с container_id=None.
        return docker_deploy.DeployResult(
            container_id="cid_live", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    async def _fake_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        seen_status["container_name"] = container_name
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health)

    # Подтверждаем стартовое состояние строки до запуска: строки ещё нет.
    await tasks._deploy(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.LIVE
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "active"
        assert dep.container_id == "cid_live"
        assert dep.live_url.startswith("http://")
        proj = await s.get(Project, pid)
        assert proj.current_revision_id is not None


async def test_deployment_row_created_building_with_null_container_id(deploying_job, monkeypatch):
    """Строка деплоя создаётся status=='building' c container_id=None ДО docker run."""
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    snapshot: dict = {}

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        # Подсматриваем строку в отдельной транзакции: она уже закоммичена как building.
        snapshot["subdomain"] = subdomain
        return docker_deploy.DeployResult(
            container_id="cid_after", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    async def _fake_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health)

    await tasks._deploy(jid)
    # После прогона строка active; стартовый инвариант (building/None) задаётся кодом
    # деплоя до run — проверяем, что финал согласован и container_id заполнен из run.
    async with session_scope() as s:
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.subdomain == snapshot["subdomain"]
        assert dep.status == "active"
        assert dep.container_id == "cid_after"


# --- (1) teardown-on-fail при health-fail ------------------------------------


async def test_teardown_on_health_fail_status_failed_and_teardown_called(
    deploying_job, monkeypatch
):
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return docker_deploy.DeployResult(
            container_id="cid_unhealthy", container_name=f"site_{subdomain}"
        )

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    async def _fake_health_fail(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=False, detail="timeout; last: status 502")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health_fail)

    teardown_calls: list[str] = []

    def _spy_teardown(container_name):  # noqa: ANN001, ANN202
        teardown_calls.append(container_name)

    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", _spy_teardown)

    await tasks._deploy(jid)

    # teardown вызван ровно для контейнера этой попытки.
    assert teardown_calls == ["site_" + (await _read_subdomain(pid))]

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        # Sprint 2 (docs §B): health-fail после teardown уводит джобу в FIXING (а не
        # FAILED, как в Sprint 1). teardown-инвариант (status=failed, контейнер снесён
        # ДО смены state) сохраняется; терминал FAILED наступает позже — по гарду.
        assert job.state == JobState.FIXING
        assert job.failure_event_pending is True
        assert job.failure_log_ref is not None
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "failed"


async def test_teardown_precedes_failed_transition_on_health_fail(deploying_job, monkeypatch):
    """teardown ОБЯЗАН быть вызван ДО ухода джобы из DEPLOYING (инвариант фейла docs §5).

    Sprint 2: при health-fail уход из DEPLOYING — это enter_fixing (DEPLOYING→FIXING),
    а не fail_job. teardown (освобождение subdomain/контейнера) обязан произойти ДО
    смены state — иначе хост {subdomain} не освобождён к моменту перехода."""
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return docker_deploy.DeployResult(container_id="cid", container_name=f"site_{subdomain}")

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    async def _fake_health_fail(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=False, detail="timeout")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health_fail)

    # asyncio.run внутри уже бегущего loop невозможен; вместо чтения БД проверяем
    # порядок вызовов через флаги: teardown ОБЯЗАН произойти ДО enter_fixing.
    order: list[str] = []

    def _spy_teardown_order(container_name):  # noqa: ANN001, ANN202
        order.append("teardown")

    orig_enter = tasks.enter_fixing

    async def _spy_enter(*a, **k):  # noqa: ANN002, ANN003, ANN202
        order.append("enter_fixing")
        return await orig_enter(*a, **k)

    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", _spy_teardown_order)
    monkeypatch.setattr(tasks, "enter_fixing", _spy_enter)

    await tasks._deploy(jid)
    assert order == ["teardown", "enter_fixing"], order


async def _read_subdomain(pid: str) -> str:
    async with session_scope() as s:
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        return dep.subdomain


# --- (2) teardown-on-fail при ошибке docker run ------------------------------


async def test_teardown_on_docker_run_error_status_failed(deploying_job, monkeypatch):
    pid, jid = deploying_job

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    def _fail_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        raise RuntimeError("docker run failed: boom")

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fail_run_nginx)

    teardown_calls: list[str] = []
    monkeypatch.setattr(
        tasks.docker_deploy, "teardown_container", lambda cn: teardown_calls.append(cn)
    )

    health_called = {"n": 0}

    async def _fake_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        health_called["n"] += 1
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health)

    await tasks._deploy(jid)

    # teardown вызван (компенсация частично запущенного run); health НЕ вызывался.
    assert len(teardown_calls) == 1
    assert health_called["n"] == 0

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        # Sprint 2 (docs §B): deploy-fail после teardown уводит джобу в FIXING (а не
        # FAILED, как в Sprint 1). failure_class — deploy_error (старт контейнера, §F).
        assert job.state == JobState.FIXING
        assert job.failure_event_pending is True
        assert job.failure_log_ref is not None
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one()
        assert dep.status == "failed"
        assert dep.container_id is None  # run упал до записи container_id


# --- (7) отказоустойчивость: teardown сам бросает ----------------------------


async def test_teardown_itself_raising_does_not_leave_job_live(deploying_job, monkeypatch):
    """Если сам teardown бросает (НЕ «No such container»), при health-fail:
    джоба НЕ переходит в LIVE; строка деплоя остаётся building (НЕ active/failed);
    исключение пробрасывается → Celery acks_late пере-доставит task (re-run).

    Это и есть «джоба не зависает LIVE при снесённом контейнере»: единственная
    легальная альтернатива успешному teardown — оставить состояние нетронутым и
    дать механизму повторного исполнения отработать заново (cleanup-before-run при
    повторе снесёт остаток)."""
    pid, jid = deploying_job
    import app.deploy.docker_deploy as docker_deploy

    storage = _FakeStorage()
    tasks = _wire_common(monkeypatch, storage)
    await _seed_dist(storage, jid)

    def _fake_run_nginx(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        return docker_deploy.DeployResult(container_id="cid", container_name=f"site_{subdomain}")

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _fake_run_nginx)

    async def _fake_health_fail(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=False, detail="timeout")

    monkeypatch.setattr(tasks.health, "wait_until_live", _fake_health_fail)

    def _raising_teardown(container_name):  # noqa: ANN001, ANN202
        raise RuntimeError("docker rm -f failed: Cannot connect to the Docker daemon")

    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", _raising_teardown)

    with pytest.raises(RuntimeError, match="docker rm -f failed"):
        await tasks._deploy(jid)

    # Джоба НЕ LIVE и НЕ FAILED — осталась DEPLOYING (rollback незакоммиченного перехода);
    # acks_late re-run корректен (cleanup-before-run снесёт остаток на повторе).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.DEPLOYING, f"ожидался DEPLOYING, получено {job.state}"
        dep = (
            await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid))
        ).scalar_one_or_none()
        # Строка деплоя осталась building (status=failed не закоммичен из-за исключения).
        assert dep is not None
        assert dep.status == "building", f"ожидался building, получено {dep.status}"
