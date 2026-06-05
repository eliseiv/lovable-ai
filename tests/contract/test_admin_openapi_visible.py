"""Contract: админ-плоскость ВИДИМА в публичной OpenAPI под тегом «Администрирование»
(ADR-021 revision, docs/modules/api §B.4/B.5/B.6).

- /v1/admin/* присутствуют в app.openapi() (include_in_schema=True на роутере);
- каждый админ-эндпоинт использует security AdminKey (НЕ глобальный BearerAuth);
- схема AdminKey (apiKey, header X-Admin-Key) объявлена в components.securitySchemes;
- тег «Администрирование» присутствует;
- Authorization НЕ выводится header-параметром операций (только security-схемы);
- внутренние маркеры (Sprint/ADR-/TD-/имена агентов) по-прежнему НЕ утекают (B.7), но
  admin/login-as/X-Admin-Key теперь легитимны (это публичная админ-поверхность).

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


def test_admin_paths_present_in_schema(schema: dict):
    """Админ-пути /v1/admin/* присутствуют в публичной схеме (include_in_schema=True)."""
    paths = schema.get("paths", {})
    assert "/v1/admin/login-as" in paths, "POST /v1/admin/login-as должен быть в публичной схеме"
    assert "/v1/admin/users/{user_id}/credits" in paths
    assert "/v1/admin/users/{user_id}" in paths


def test_admin_operations_use_adminkey_security(schema: dict):
    """Каждая операция /v1/admin/* несёт security=[{AdminKey: []}], НЕ BearerAuth."""
    for path, item in schema.get("paths", {}).items():
        if not path.startswith("/v1/admin/"):
            continue
        for method, op in item.items():
            if not isinstance(op, dict) or method not in ("get", "post", "put", "delete", "patch"):
                continue
            assert op.get("security") == [{"AdminKey": []}], (
                f"{method.upper()} {path} должен требовать AdminKey (X-Admin-Key), не Bearer"
            )


def test_adminkey_scheme_defined(schema: dict):
    """В components.securitySchemes объявлена AdminKey (apiKey в заголовке X-Admin-Key)."""
    schemes = schema.get("components", {}).get("securitySchemes", {})
    admin = schemes.get("AdminKey")
    assert admin is not None, "Схема AdminKey должна быть объявлена"
    assert admin.get("type") == "apiKey"
    assert admin.get("in") == "header"
    assert admin.get("name") == "X-Admin-Key"
    # Глобальная BearerAuth тоже на месте (для обычных эндпоинтов).
    assert "BearerAuth" in schemes


def test_admin_tag_present(schema: dict):
    """Тег «Администрирование» присутствует в метаданных схемы."""
    tag_names = {t.get("name") for t in schema.get("tags", [])}
    assert "Администрирование" in tag_names


def test_non_admin_endpoints_keep_bearer(schema: dict):
    """Обычные эндпоинты не переопределяют security на AdminKey (наследуют глобальный Bearer)."""
    assert schema.get("security") == [{"BearerAuth": []}]
    projects = schema.get("paths", {}).get("/v1/projects", {})
    # У /v1/projects нет per-operation AdminKey-override.
    for op in projects.values():
        if isinstance(op, dict):
            assert op.get("security") != [{"AdminKey": []}]


def test_authorization_not_a_header_parameter(schema: dict):
    """Authorization НЕ выводится header-параметром операций (только security-схемы)."""
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            for param in op.get("parameters", []):
                if param.get("in") == "header":
                    assert param.get("name", "").lower() != "authorization", (
                        "Authorization не должен быть явным header-параметром операции"
                    )


@pytest.mark.parametrize("forbidden", ["adr-", "sprint", "td-", "interviewer", "builder", "fixer"])
def test_b7_internal_markers_still_absent(schema_json: str, forbidden: str):
    """B.7: внутренние маркеры по-прежнему отсутствуют (admin/login-as теперь легитимны)."""
    assert forbidden not in schema_json.lower(), (
        f"Внутренний маркер '{forbidden}' утёк в публичную OpenAPI-схему (B.7)"
    )
