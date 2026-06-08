"""Contract: публичная OpenAPI-схема для register/login/secret (ADR-024, тема B.7).

docs/modules/auth/02-api-contracts.md, docs/06-testing-strategy §Contract, app/api/main.py
(_custom_openapi). Граница: чистая сериализация app.openapi() — без БД/сети.

Покрывает:
- /auth/register и /auth/login имеют security: [] (публичные, без auth);
- /auth/secret под BearerAuth;
- /admin/* под AdminKey (если присутствуют);
- нет запрещённых B.7-подстрок (Sprint/ADR-/имён агентов) — на новых путях;
- эндпоинты помечены тегом «Аутентификация».
"""

from __future__ import annotations

import json

import pytest

from app.api.main import app


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


def _post_op(schema: dict, path: str) -> dict:
    item = schema["paths"].get(path)
    assert item is not None, f"путь {path} отсутствует в OpenAPI-схеме"
    op = item.get("post")
    assert op is not None, f"POST {path} отсутствует"
    return op


def test_register_security_empty(schema):
    op = _post_op(schema, "/v1/auth/register")
    assert op.get("security") == []  # публичный, без Bearer


def test_login_security_empty(schema):
    op = _post_op(schema, "/v1/auth/login")
    assert op.get("security") == []


def test_secret_under_bearer_auth(schema):
    op = _post_op(schema, "/v1/auth/secret")
    # security НЕ снят (глобальный BearerAuth действует) ИЛИ явно BearerAuth.
    sec = op.get("security")
    if sec is None:
        assert schema.get("security") == [{"BearerAuth": []}]  # наследует глобальный
    else:
        assert sec == [{"BearerAuth": []}]
    assert sec != []  # точно НЕ публичный


def test_admin_paths_under_admin_key(schema):
    admin_paths = [p for p in schema["paths"] if p.startswith("/v1/admin/")]
    for p in admin_paths:
        for op in schema["paths"][p].values():
            if isinstance(op, dict) and "security" in op:
                assert op["security"] == [{"AdminKey": []}], p


def test_auth_endpoints_tagged_authentication(schema):
    for path in ("/v1/auth/register", "/v1/auth/login", "/v1/auth/secret"):
        op = _post_op(schema, path)
        assert "Аутентификация" in op.get("tags", []), path


def test_no_internal_markers_on_new_auth_paths(schema):
    """B.7: на путях register/login/secret нет внутренних маркеров (ADR-/Sprint/имена)."""
    blob = json.dumps(
        {p: schema["paths"][p] for p in ("/v1/auth/register", "/v1/auth/login", "/v1/auth/secret")},
        ensure_ascii=False,
    ).lower()
    for forbidden in ("sprint", "спринт", "adr-", "td-", "interviewer", "builder", "fixer"):
        assert forbidden not in blob, f"маркер '{forbidden}' утёк в схему auth-путей"
