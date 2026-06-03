"""Unit/contract guard: x-app-env ключи compose ⊆ поля Settings (TD-002).

Settings(extra="ignore") молча глотает несовпадающие env-имена → тихий дефолт в проде
(docs/07-deployment.md "Контракт переменных окружения"). Этот гард ловит опечатку
в именах ДО рантайма: каждый ключ x-app-env (кроме явно задокументированных
не-Settings ключей alembic/celery) обязан совпасть с полем Settings в upper-case.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.config import Settings

_COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.dev.yml"

# Ключи, которые ЯВНО не являются полями Settings (читаются alembic/celery напрямую,
# docs/07-deployment.md стр. 65). Допустимы в x-app-env, Settings их игнорирует.
_NON_SETTINGS_KEYS = {
    "DATABASE_URL_SYNC",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
}


def _load_app_env_keys() -> set[str]:
    raw = _COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    app_env = doc["x-app-env"]
    return set(app_env.keys())


def test_compose_app_env_keys_match_settings_fields():
    settings_fields = {name.upper() for name in Settings.model_fields}
    app_env_keys = _load_app_env_keys()

    unknown = {k for k in app_env_keys if k not in settings_fields and k not in _NON_SETTINGS_KEYS}
    assert not unknown, (
        f"x-app-env ключи без соответствующего поля Settings (тихий дефолт в проде): {unknown}. "
        "Settings(extra=ignore) их проглотит — это блокер контракта (docs/07-deployment.md)."
    )


def test_non_settings_keys_are_truly_not_settings_fields():
    # Гарантия, что список исключений не маскирует реальное поле Settings.
    settings_fields = {name.upper() for name in Settings.model_fields}
    for key in _NON_SETTINGS_KEYS:
        assert key not in settings_fields, f"{key} оказался полем Settings — убери из исключений"


def test_critical_settings_fields_are_present_in_compose():
    # Ключевые поля контракта обязаны присутствовать в x-app-env (не потеряны).
    app_env_keys = _load_app_env_keys()
    for required in (
        "DATABASE_URL",
        "REDIS_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "ANTHROPIC_API_KEY",
        "SEED_API_KEY",
        "APPS_DOMAIN",
        "TRAEFIK_NETWORK",
        "NGINX_IMAGE",
    ):
        assert required in app_env_keys, f"{required} отсутствует в x-app-env compose"
