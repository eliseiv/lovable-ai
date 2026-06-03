"""Unit: Traefik-лейблы, live_url, health-check URL по окружению.

docs/modules/deploy/03-architecture.md.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.deploy.health import _check_url
from app.deploy.traefik import live_url, traefik_labels


def _dev_settings():  # noqa: ANN201
    return get_settings().model_copy(update={"environment": "dev", "apps_domain": "apps.localhost"})


def _prod_settings():  # noqa: ANN201
    return get_settings().model_copy(update={"environment": "prod", "apps_domain": "apps.domain"})


SUB = "abcdef0123456789"


# --- traefik labels ---


def test_traefik_labels_dev_http_no_tls():
    labels = traefik_labels(_dev_settings(), SUB)
    router = f"traefik.http.routers.{SUB}"
    service = f"traefik.http.services.{SUB}"
    assert labels["traefik.enable"] == "true"
    assert labels[f"{router}.rule"] == f"Host(`{SUB}.apps.localhost`)"
    assert labels[f"{router}.entrypoints"] == "web"
    assert labels[f"{service}.loadbalancer.server.port"] == "80"
    # Без TLS/certresolver в dev.
    assert f"{router}.tls" not in labels
    assert f"{router}.tls.certresolver" not in labels


def test_traefik_labels_prod_websecure_tls_certresolver():
    labels = traefik_labels(_prod_settings(), SUB)
    router = f"traefik.http.routers.{SUB}"
    assert labels[f"{router}.entrypoints"] == "websecure"
    assert labels[f"{router}.tls"] == "true"
    assert labels[f"{router}.tls.certresolver"] == "letsencrypt"
    assert labels[f"{router}.rule"] == f"Host(`{SUB}.apps.domain`)"


# --- live_url ---


def test_live_url_dev_http():
    assert live_url(_dev_settings(), SUB) == f"http://{SUB}.apps.localhost/"


def test_live_url_prod_https():
    assert live_url(_prod_settings(), SUB) == f"https://{SUB}.apps.domain/"


# --- health check url ---


def test_health_url_dev_internal_http_no_verify():
    url, verify = _check_url(_dev_settings(), SUB, "site_container_x")
    assert url == "http://site_container_x:80/"
    assert verify is False


def test_health_url_prod_public_https_verify():
    url, verify = _check_url(_prod_settings(), SUB, "site_container_x")
    assert url == f"https://{SUB}.apps.domain/"
    assert verify is True


def test_dev_three_planes_consistent_http():
    """Dev-консистентность: traefik entrypoint web, live_url http, health http — все http."""
    s = _dev_settings()
    labels = traefik_labels(s, SUB)
    assert labels[f"traefik.http.routers.{SUB}.entrypoints"] == "web"
    assert live_url(s, SUB).startswith("http://")
    url, verify = _check_url(s, SUB, "c")
    assert url.startswith("http://")
    assert verify is False
