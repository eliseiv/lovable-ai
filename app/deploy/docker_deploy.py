"""Деплой dist/ в generic nginx-контейнер с Traefik-лейблами (ADR-002).

dist/ → /srv/sites/{pid}/ (read-only mount), docker run nginx:alpine с Traefik-лейблами.
Лейблы ветвятся по SITE_ROUTING_MODE (app/deploy/routing.py): subdomain → Host-router
`{subdomain}.apps.domain`; path (prod, ADR-017) → PathPrefix(`/s/{site_id}`)+StripPrefix.
Сеть контейнера — settings.traefik_network (env TRAEFIK_NETWORK; prod = web, external,
ADR-018) — не хардкод. container_id сохраняется в site_deployments.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.deploy.traefik import traefik_labels

logger = get_logger(__name__)


@dataclass(frozen=True)
class DeployResult:
    container_id: str
    container_name: str


def _site_dir(settings: Settings, project_id: str) -> Path:
    return Path(settings.sites_host_root) / project_id


def teardown_container(container_name: str) -> None:
    """Идемпотентный снос контейнера сайта (`docker rm -f`).

    Используется в двух местах (docs/modules/deploy/03-architecture.md §5):
      - cleanup-before-run: перед `docker run` — снос возможного остатка с тем же
        именем (crash-resume / Celery acks_late / будущий FIXING→DEPLOYING), иначе
        повтор упирается в name-collision;
      - teardown-on-fail: при ошибке `docker run` / провале health-check — снос уже
        запущенного контейнера текущей попытки (снимает `--restart unless-stopped`,
        Traefik-route убирается автоматически через Docker-лейблы).

    Идемпотентно: отсутствие контейнера («No such container») — не ошибка.
    argv фиксирован (без shell), как в `run_nginx_container`.
    """
    argv: list[str] = ["docker", "rm", "-f", container_name]
    logger.info("site_container_teardown", extra={"container_name": container_name})
    completed = subprocess.run(  # noqa: S603 - argv фиксирован, без shell
        argv, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        # `docker rm -f` на отсутствующем контейнере возвращает ненулевой код с
        # "No such container" — это ожидаемо для идемпотентного teardown, не ошибка.
        if "No such container" in stderr or "no such container" in stderr:
            logger.info(
                "site_container_teardown_absent",
                extra={"container_name": container_name},
            )
            return
        raise RuntimeError(f"docker rm -f failed: {stderr}")


def publish_dist(settings: Settings, project_id: str, dist_dir: Path) -> Path:
    """Копирует dist/ в хостовый каталог сайта (монтируется в nginx)."""
    target = _site_dir(settings, project_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(dist_dir, target)
    return target


def run_nginx_container(
    settings: Settings,
    *,
    project_id: str,
    subdomain: str,
    site_dir: Path,
) -> DeployResult:
    """`docker run nginx:alpine` с примонтированным dist/ (ro) и Traefik-лейблами.

    Cleanup-before-run (docs §5): перед `docker run` идемпотентно сносим возможный
    остаток контейнера с тем же детерминированным именем `site_{subdomain}`, чтобы
    повторный прогон (crash-resume / Celery acks_late / FIXING→DEPLOYING) не упирался
    в name-collision (`Conflict. The container name is already in use`).
    """
    container_name = f"site_{subdomain}"
    teardown_container(container_name)
    argv: list[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--restart",
        "unless-stopped",
        "--network",
        settings.traefik_network,
        "-v",
        f"{site_dir}:/usr/share/nginx/html:ro",
    ]
    for key, value in traefik_labels(settings, subdomain).items():
        argv.extend(["--label", f"{key}={value}"])
    argv.append(settings.nginx_image)

    logger.info(
        "site_container_run",
        extra={"project_id": project_id, "subdomain": subdomain, "container_name": container_name},
    )
    completed = subprocess.run(  # noqa: S603 - argv фиксирован, без shell
        argv, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker run failed: {completed.stderr.strip()}")
    container_id = completed.stdout.strip()
    return DeployResult(container_id=container_id, container_name=container_name)
