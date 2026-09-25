"""Integration: повторный деплой и инфраструктурный отказ (ADR-055).

Прод-инцидент 2026-09-25 (nexoraweb, job j_z77y946kqxv7nxylag9go0v9): в сети `web` кончились
IPv4-адреса → `docker run` не поднял nginx. Дальше сложилось два дефекта:
  - фейл ушёл в fix-loop, хотя патч кода сайта такое не чинит (виток Agent 4 за деньги зря);
  - повторный деплой той же джобы вставлял ВТОРУЮ строку `site_deployments` с тем же
    стабильным `subdomain` → UniqueViolation → ретраи Celery исчерпаны → `infra_error`,
    в котором настоящая причина не видна.
Здесь оба пути закрыты тестом.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, SiteDeployment
from app.db.session import session_scope
from app.deploy.docker_deploy import DockerInfraUnavailable
from app.pipeline.failure_signature import parse_failure_log
from app.storage import s3
from tests.integration.test_deploying_to_fixing import (  # noqa: F401 — фикстура переиспользуется
    _dist_tgz,
    _wire_deploy,
    job_in_state,
)

pytestmark = pytest.mark.asyncio


def _ok_run(deploy_results: list[str]):  # noqa: ANN202
    def _run(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        deploy_results.append(subdomain)

        class _R:
            container_id = "cid-" + subdomain
            container_name = "site_" + subdomain

        return _R()

    return _run


async def test_second_deploy_of_same_job_reuses_deployment_row(job_in_state, monkeypatch):  # noqa: F811
    """Тот же job → тот же стабильный site_id → строка деплоя переиспользуется, не дублируется."""
    make, storage = job_in_state
    pid, jid = await make(JobState.DEPLOYING)
    tasks = _wire_deploy(monkeypatch, storage)
    storage.objects[s3.dist_key(jid)] = _dist_tgz()
    # Конфликт возможен только в path-режиме (прод): там site_id джобы стабилен.
    monkeypatch.setattr(get_settings(), "site_routing_mode", "path", raising=False)

    subdomains: list[str] = []
    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _ok_run(subdomains))
    monkeypatch.setattr(tasks.docker_deploy, "teardown_container", lambda cn: None)
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)
    monkeypatch.setattr(tasks.workspace, "safe_extract_tgz", lambda data, ws: None)

    async def _ok_health(settings, *, subdomain, container_name):  # noqa: ANN001, ANN202
        return tasks.health.HealthResult(ok=True, detail="200")

    monkeypatch.setattr(tasks.health, "wait_until_live", _ok_health)

    await tasks._deploy(jid)

    # Второй деплой той же джобы (fix-loop вернул её в DEPLOYING / crash-resume).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        job.state = JobState.DEPLOYING
        await s.commit()

    await tasks._deploy(jid)

    async with session_scope() as s:
        rows = (
            (await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid)))
            .scalars()
            .all()
        )
        job = await s.get(GenerationJob, jid)

    # В path-режиме site_id стабилен, поэтому строка ровно одна — вторая вставка упиралась бы
    # в uq_site_deployments_subdomain (прод-инцидент).
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert job.state == JobState.LIVE


async def test_infra_failure_fails_job_without_fix_loop(job_in_state, monkeypatch):  # noqa: F811
    """Нет адресов в сети — это состояние хоста: сразу FAILED(infra_error), без Agent 4."""
    make, storage = job_in_state
    pid, jid = await make(JobState.DEPLOYING)
    tasks = _wire_deploy(monkeypatch, storage)
    storage.objects[s3.dist_key(jid)] = _dist_tgz()

    def _no_addresses(settings, *, project_id, subdomain, site_dir):  # noqa: ANN001, ANN202
        raise DockerInfraUnavailable(
            "docker run failed: docker: Error response from daemon: failed to set up container "
            "networking: no available IPv4 addresses on this network's address pools: web"
        )

    order: list[str] = []
    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _no_addresses)
    monkeypatch.setattr(
        tasks.docker_deploy, "teardown_container", lambda cn: order.append("teardown")
    )
    monkeypatch.setattr(tasks.sandbox, "cleanup_workspace", lambda ws: None)
    monkeypatch.setattr(tasks.workspace, "safe_extract_tgz", lambda data, ws: None)

    async def _spy_enter(*a, **k):  # noqa: ANN002, ANN003, ANN202
        order.append("enter_fixing")

    monkeypatch.setattr(tasks, "enter_fixing", _spy_enter)

    await tasks._deploy(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        events = (
            (
                await s.execute(
                    select(JobEvent.event_type).where(
                        JobEvent.job_id == jid,
                        JobEvent.event_type.in_(("deploy_infra_unavailable", "build_failed")),
                    )
                )
            )
            .scalars()
            .all()
        )
        deployments = (
            await s.execute(
                select(func.count())
                .select_from(SiteDeployment)
                .where(SiteDeployment.project_id == pid, SiteDeployment.status == "failed")
            )
        ).scalar_one()

    assert job.state == JobState.FAILED
    assert job.failure_reason == "infra_error"
    # В fix-loop не пошли: ни enter_fixing, ни build_failed-события.
    assert order == ["teardown"]
    assert events == ["deploy_infra_unavailable"]
    assert deployments == 1
    # Причина сохранена в логе попытки — по нему инцидент разбирается без гадания.
    log = storage.objects[s3.deploy_log_key(jid, 0)].decode()
    assert parse_failure_log(log).failure_class == "infra_error"
    assert "no available IPv4 addresses" in log
