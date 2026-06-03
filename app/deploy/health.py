"""Health-check сайта до 200/timeout (docs/modules/deploy/03-architecture.md §4/§2A).

Цель health-check ветвится по SITE_ROUTING_MODE (единый источник — app/deploy/routing.py):
  - subdomain dev: внутренний http к контейнеру по имени сети, TLS-verify off;
  - subdomain prod: https к {subdomain}.apps.domain (полная TLS-верификация);
  - path (prod, ADR-017): https://{apps_domain}/s/{site_id}/ через общий edge-Traefik
    (StripPrefix снимает /s/{id} → nginx отдаёт index по /; ассеты по /s/{id}/ за счёт
    Vite --base). Связано с Q-DEPLOY-2 (resolved path-based).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.deploy import routing

logger = get_logger(__name__)


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str


def _check_url(settings: Settings, subdomain: str, container_name: str) -> tuple[str, bool]:
    """Возвращает (url, verify_tls) по режиму (единый источник — routing.health_check_target).

    path: https://{apps_domain}/s/{site_id}/ (общий Traefik, TLS-verify on). subdomain dev:
    http к nginx-контейнеру по имени в compose-сети (TLS off). subdomain prod: https к хосту.
    """
    return routing.health_check_target(settings, subdomain, container_name)


async def wait_until_live(
    settings: Settings, *, subdomain: str, container_name: str
) -> HealthResult:
    """Опрашивает сайт до HTTP 200 или таймаута."""
    url, verify_tls = _check_url(settings, subdomain, container_name)
    deadline = time.monotonic() + settings.health_check_timeout_s
    timeout = httpx.Timeout(
        settings.health_check_connect_timeout_s,
        connect=settings.health_check_connect_timeout_s,
    )
    last_detail = "no attempt"
    async with httpx.AsyncClient(verify=verify_tls, timeout=timeout) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail=f"200 from {url}")
                last_detail = f"status {resp.status_code} from {url}"
            except (httpx.HTTPError, OSError) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(settings.health_check_interval_s)
    logger.warning("health_check_timeout", extra={"subdomain": subdomain, "detail": last_detail})
    return HealthResult(ok=False, detail=f"timeout; last: {last_detail}")
