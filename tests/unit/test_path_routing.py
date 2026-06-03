"""Unit/integration: path-based site routing (SITE_ROUTING_MODE=path, ADR-017).

Покрывает path-режим в дополнение к субдомен-сьюту (tests/unit/test_traefik_health_routing.py).
Нормативный источник: docs/06-testing-strategy.md → "Path-based routing", ADR-017,
docs/modules/deploy/03-architecture.md §2A. Единый источник ветвления — app/deploy/routing.py;
traefik.py/health.py делегируют в него (регресс-проверка делегирования включена).

Граница: чистые функции routing.* и _resolve_site_id (job_events) — без Docker/сети/сборки.
"""

from __future__ import annotations

import re

import pytest

from app.core.config import get_settings
from app.deploy import routing

SITE = "abcdef0123456789"


def _path_settings():  # noqa: ANN201
    """prod-настройки в path-режиме: routing_is_path == True, apps_domain зафиксирован."""
    return get_settings().model_copy(
        update={
            "environment": "prod",
            "site_routing_mode": "path",
            "apps_domain": "apps.domain",
        }
    )


def _subdomain_settings():  # noqa: ANN201
    """dev-настройки в subdomain-режиме (дефолт): routing_is_path == False (регресс)."""
    return get_settings().model_copy(
        update={
            "environment": "dev",
            "site_routing_mode": "subdomain",
            "apps_domain": "apps.localhost",
        }
    )


def _subdomain_prod_settings():  # noqa: ANN201
    """prod в subdomain-режиме (Host+TLS) — регресс, что path не активируется по prod, а по mode."""
    return get_settings().model_copy(
        update={
            "environment": "prod",
            "site_routing_mode": "subdomain",
            "apps_domain": "apps.domain",
        }
    )


# ===========================================================================
# traefik_labels — path: PathPrefix + StripPrefix-middleware + websecure + port.
# ===========================================================================


def test_traefik_labels_path_pathprefix_stripprefix_websecure():
    labels = routing.traefik_labels(_path_settings(), SITE)
    router = f"traefik.http.routers.{SITE}"
    service = f"traefik.http.services.{SITE}"
    middleware = f"{SITE}-strip"
    prefix = f"/s/{SITE}"

    # PathPrefix-роутер (не Host).
    assert labels[f"{router}.rule"] == f"PathPrefix(`{prefix}`)"
    assert "Host(" not in labels[f"{router}.rule"]
    # websecure-entrypoint (общий edge-Traefik терминирует TLS, ADR-018).
    assert labels[f"{router}.entrypoints"] == "websecure"
    # StripPrefix-middleware: nginx внутри получает / (а не /s/{site_id}).
    assert labels[f"traefik.http.middlewares.{middleware}.stripprefix.prefixes"] == prefix
    assert labels[f"{router}.middlewares"] == middleware
    # Порт сервиса (generic nginx:alpine).
    assert labels[f"{service}.loadbalancer.server.port"] == "80"
    assert labels["traefik.enable"] == "true"
    # В path-режиме НЕТ Host-tls-лейблов (TLS на edge, не на сайт-роутере).
    assert f"{router}.tls" not in labels
    assert f"{router}.tls.certresolver" not in labels


def test_traefik_labels_subdomain_host_regression():
    """Субдомен-режим: Host-router (регресс — path не сломал субдомен)."""
    labels = routing.traefik_labels(_subdomain_settings(), SITE)
    router = f"traefik.http.routers.{SITE}"
    assert labels[f"{router}.rule"] == f"Host(`{SITE}.apps.localhost`)"
    assert labels[f"{router}.entrypoints"] == "web"
    # Нет StripPrefix-middleware в субдомен-режиме.
    assert f"traefik.http.middlewares.{SITE}-strip.stripprefix.prefixes" not in labels
    assert f"{router}.middlewares" not in labels


def test_traefik_labels_subdomain_prod_host_tls_regression():
    """Субдомен prod: Host + TLS + certresolver (режим решает mode, не только окружение)."""
    labels = routing.traefik_labels(_subdomain_prod_settings(), SITE)
    router = f"traefik.http.routers.{SITE}"
    assert labels[f"{router}.rule"] == f"Host(`{SITE}.apps.domain`)"
    assert labels[f"{router}.entrypoints"] == "websecure"
    assert labels[f"{router}.tls"] == "true"
    assert labels[f"{router}.tls.certresolver"] == "letsencrypt"


# ===========================================================================
# live_url — path: https://{APPS_DOMAIN}/s/{site_id}/ ; subdomain: {scheme}://{sub}.{domain}/
# ===========================================================================


def test_live_url_path_with_trailing_slash():
    assert routing.live_url(_path_settings(), SITE) == f"https://apps.domain/s/{SITE}/"


def test_live_url_subdomain_dev_http_regression():
    assert routing.live_url(_subdomain_settings(), SITE) == f"http://{SITE}.apps.localhost/"


def test_live_url_subdomain_prod_https_regression():
    assert routing.live_url(_subdomain_prod_settings(), SITE) == f"https://{SITE}.apps.domain/"


# ===========================================================================
# augment_build_command — path: добавляет --base=/s/{site_id}/ ; subdomain: без base.
# ===========================================================================


def test_augment_build_command_path_adds_base():
    cmd = "npm ci && npm run build"
    out = routing.augment_build_command(_path_settings(), cmd, SITE)
    assert out == f"npm ci && npm run build --base=/s/{SITE}/"
    assert out.endswith(f"--base=/s/{SITE}/")


def test_augment_build_command_subdomain_unchanged():
    cmd = "npm ci && npm run build"
    out = routing.augment_build_command(_subdomain_settings(), cmd, SITE)
    assert out == cmd
    assert "--base" not in out


def test_vite_base_helper_format():
    assert routing.vite_base(SITE) == f"/s/{SITE}/"
    assert routing.site_path_prefix(SITE) == f"/s/{SITE}"


# ===========================================================================
# health_check_target — path: {APPS_DOMAIN}/s/{site_id}/ (TLS-verify True).
# ===========================================================================


def test_health_check_target_path():
    url, verify = routing.health_check_target(_path_settings(), SITE, "site_container_x")
    assert url == f"https://apps.domain/s/{SITE}/"
    assert verify is True


def test_health_check_target_subdomain_dev_internal_regression():
    url, verify = routing.health_check_target(_subdomain_settings(), SITE, "site_container_x")
    assert url == "http://site_container_x:80/"
    assert verify is False


def test_health_check_target_path_equals_live_url():
    """Path: health-цель == live_url (один путь через edge-Traefik, §2A/§4)."""
    s = _path_settings()
    url, _ = routing.health_check_target(s, SITE, "ignored")
    assert url == routing.live_url(s, SITE)


def test_path_three_planes_consistent():
    """Path-консистентность: router-rule, live_url, health — один и тот же /s/{site_id}-путь."""
    s = _path_settings()
    labels = routing.traefik_labels(s, SITE)
    prefix = f"/s/{SITE}"
    assert labels[f"traefik.http.routers.{SITE}.rule"] == f"PathPrefix(`{prefix}`)"
    assert routing.live_url(s, SITE) == f"https://apps.domain{prefix}/"
    url, verify = routing.health_check_target(s, SITE, "c")
    assert url == f"https://apps.domain{prefix}/"
    assert verify is True


# ===========================================================================
# Делегирование traefik.py/health.py → routing.py (единый источник, регресс).
# ===========================================================================


def test_traefik_module_delegates_to_routing_path():
    from app.deploy.traefik import live_url as t_live_url
    from app.deploy.traefik import traefik_labels as t_labels

    s = _path_settings()
    assert t_labels(s, SITE) == routing.traefik_labels(s, SITE)
    assert t_live_url(s, SITE) == routing.live_url(s, SITE)


def test_health_module_delegates_to_routing_path():
    from app.deploy.health import _check_url

    s = _path_settings()
    assert _check_url(s, SITE, "cx") == routing.health_check_target(s, SITE, "cx")


# ===========================================================================
# site_id стабильность build↔deploy: _resolve_site_id читает один site_id по job_id.
# Integration: реальный Postgres (job_events), append-only персист (ADR-017 §2A).
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_site_id_stable_across_calls(session, seeded_user):
    """build↔deploy: один job_id → один site_id (персист в job_events.site_id_assigned)."""
    from app.db.models import GenerationJob, Project
    from app.workers.tasks import _SITE_ID_ASSIGNED_EVENT, _resolve_site_id

    project = Project(
        id="p_pathrouting0000000000",
        user_id=seeded_user.id,
        prompt="path-routing-test",
    )
    session.add(project)
    await session.flush()
    job = GenerationJob(
        id="j_pathrouting000000000a",
        project_id=project.id,
        user_id=seeded_user.id,
        kind="generation",
        state="BUILDING",
    )
    session.add(job)
    await session.flush()

    # Фаза build: первый вызов генерирует opaque [a-z0-9]{16} и пишет site_id_assigned.
    # Прод-контракт (_build_request): вызывающий коммитит site_id_assigned сразу после
    # resolve — иначе append-only event не виден последующему SELECT (record_event не
    # флашит сам). В тест-транзакции эквивалент коммита — flush (SAVEPOINT-изоляция).
    site_id_build = await _resolve_site_id(session, job.id)
    await session.flush()
    assert re.fullmatch(r"[a-z0-9]{16}", site_id_build)

    # Фаза deploy / fix-loop rebuild / crash-resume: повторные вызовы → ТОТ ЖЕ site_id.
    site_id_deploy = await _resolve_site_id(session, job.id)
    await session.flush()
    site_id_resume = await _resolve_site_id(session, job.id)
    await session.flush()
    assert site_id_deploy == site_id_build
    assert site_id_resume == site_id_build

    # Ровно один site_id_assigned-event в job_events (идемпотентность записи).
    from sqlalchemy import func, select

    from app.db.models import JobEvent

    count = (
        await session.execute(
            select(func.count())
            .select_from(JobEvent)
            .where(
                JobEvent.job_id == job.id,
                JobEvent.event_type == _SITE_ID_ASSIGNED_EVENT,
            )
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_resolve_site_id_distinct_per_job(session, seeded_user):
    """Разные job_id → разные site_id (opaque, не реюзается между джобами)."""
    from app.db.models import GenerationJob, Project
    from app.workers.tasks import _resolve_site_id

    project = Project(
        id="p_pathrouting0000000001",
        user_id=seeded_user.id,
        prompt="path-routing-test-2",
    )
    session.add(project)
    await session.flush()
    ids = []
    for suffix in ("b", "c"):
        job = GenerationJob(
            id=f"j_pathrouting000000000{suffix}",
            project_id=project.id,
            user_id=seeded_user.id,
            kind="generation",
            state="BUILDING",
        )
        session.add(job)
        await session.flush()
        ids.append(await _resolve_site_id(session, job.id))
        await session.flush()
    assert ids[0] != ids[1]
