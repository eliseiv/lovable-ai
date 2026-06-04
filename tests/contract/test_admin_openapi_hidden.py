"""Contract: админ-плоскость ADR-021 скрыта из публичной OpenAPI (docs/admin §4, api §B.5/B.7).

- /v1/admin/* отсутствуют в app.openapi() (include_in_schema=False на роутере);
- Authorization НЕ выводится header-параметром операций (только глобальный BearerAuth);
- B.7-денилист чист по admin-маркерам: 'login-as', 'X-Admin-Key', 'admin' в путях, 'Sprint',
  'ADR' не утекают в /openapi.json.

Граница: чистая сериализация app.openapi() — без БД/сети.
"""

from __future__ import annotations

import json

import pytest

from app.api.main import app


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


@pytest.fixture(scope="module")
def schema_json(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False)


def test_admin_paths_absent_from_schema(schema: dict):
    """Ни один /v1/admin/* путь не попадает в публичную схему (include_in_schema=False)."""
    for path in schema.get("paths", {}):
        assert "/admin/" not in path, f"Админ-путь {path} утёк в публичную OpenAPI-схему"


def test_login_as_and_credits_operations_absent(schema_json: str):
    """Конкретные админ-эндпоинты (login-as / credits) не сериализованы в схему."""
    blob = schema_json.lower()
    assert "login-as" not in blob
    assert "/credits" not in blob


def test_x_admin_key_header_not_in_schema(schema_json: str):
    """Заголовок X-Admin-Key не утекает в публичную схему."""
    assert "x-admin-key" not in schema_json.lower()


def test_authorization_not_a_header_parameter(schema: dict):
    """Authorization НЕ выводится header-параметром операций (только глобальный BearerAuth).

    get_current_user читает Authorization через Header(include_in_schema=False); явный
    header-параметр дублировал бы глобальную BearerAuth-кнопку (dependencies.py docstring).
    """
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            for param in op.get("parameters", []):
                if param.get("in") == "header":
                    assert param.get("name", "").lower() != "authorization", (
                        "Authorization не должен быть явным header-параметром операции"
                    )


@pytest.mark.parametrize("forbidden", ["adr", "sprint", "x-admin-key", "login-as"])
def test_b7_denylist_clean_for_admin_markers(schema_json: str, forbidden: str):
    """B.7: admin/ADR/Sprint-маркеры (case-insensitive) отсутствуют в /openapi.json."""
    assert forbidden not in schema_json.lower(), (
        f"Запрещённая подстрока '{forbidden}' утекла в публичную OpenAPI-схему (B.7)"
    )
