"""Формирование Traefik-лейблов и live_url сайта по SITE_ROUTING_MODE (ADR-017).

Единый источник ветвления subdomain/path — app/deploy/routing.py. Этот модуль сохраняет
прежний публичный API (traefik_labels/live_url/site_host), делегируя routing-логику в
routing.py, чтобы не дублировать ветвление по режиму в нескольких местах.

Режимы (docs/modules/deploy/03-architecture.md §2/§2A):
  - subdomain (dev по умолчанию): Host(`{subdomain}.{apps_domain}`); entrypoint/TLS из
    Settings.sites_use_tls (dev — web/http, prod — websecure/tls + certResolver).
  - path (prod, ADR-017): PathPrefix(`/s/{site_id}`) + StripPrefix-middleware +
    entrypoints=websecure; live_url=https://{apps_domain}/s/{site_id}/.

site_id = opaque [a-z0-9]{16} = site_deployments.subdomain (single normative source колонки).
"""

from __future__ import annotations

from app.core.config import Settings
from app.deploy import routing


def site_host(subdomain: str, apps_domain: str) -> str:
    """Хост сайта в subdomain-режиме `{subdomain}.{apps_domain}` (§2)."""
    return f"{subdomain}.{apps_domain}"


def live_url(settings: Settings, subdomain: str) -> str:
    """LIVE URL сайта по режиму (единый источник — routing.live_url, §2A/§4)."""
    return routing.live_url(settings, subdomain)


def traefik_labels(settings: Settings, subdomain: str) -> dict[str, str]:
    """Лейблы Traefik Docker-провайдера для nginx-контейнера сайта по режиму.

    Делегирует в routing.traefik_labels: path → PathPrefix+StripPrefix+websecure (§2A);
    subdomain → Host-router + entrypoint/TLS из Settings.sites_use_tls (§3).
    """
    return routing.traefik_labels(settings, subdomain)
