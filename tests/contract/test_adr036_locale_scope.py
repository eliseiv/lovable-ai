"""Contract: scope явного locale — ТОЛЬКО POST /projects, НЕ /edits (ADR-036 §2).

Источник истины — ADR-036 §2/§3 + docs/modules/api/02-api-contracts.md (POST /projects:
Form-поле `locale` опц.; «контракт POST /projects/{pid}/edits `locale` НЕ принимает»).
Правка наследует язык сайта через маркер `**Content language:**`, не переопределяет его.

Проверяется по фактической OpenAPI-схеме FastAPI (app.openapi()) — реальный контракт, не
комментарий: request body multipart POST /projects содержит свойство `locale`; request body
POST /projects/{project_id}/edits его НЕ содержит. Плюс behaviour-проверка: /edits с полем
`locale` в форме не падает 422 ИЗ-ЗА locale (поле просто игнорируется как лишнее), а отрабатывает
штатный путь (тут — 404 на несуществующий проект), т.е. контракт /edits не изменился.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _request_body_props(openapi: dict, path: str, method: str = "post") -> set[str]:
    """Имена свойств request-body schema операции (multipart/form-data) из OpenAPI."""
    op = openapi["paths"][path][method]
    content = op["requestBody"]["content"]
    # multipart/form-data — транспорт POST /projects и /edits (ADR-034).
    media = content.get("multipart/form-data") or next(iter(content.values()))
    schema = media["schema"]
    # FastAPI инлайнит form-схему (или $ref на components). Развернём $ref при необходимости.
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        schema = openapi["components"]["schemas"][ref]
    return set(schema.get("properties", {}).keys())


async def test_openapi_post_projects_has_locale_field():
    """POST /v1/projects request-body содержит опц. Form-поле `locale` (ADR-036 §3)."""
    from app.api.main import app

    openapi = app.openapi()
    props = _request_body_props(openapi, "/v1/projects")
    assert "locale" in props, f"POST /projects обязан нести Form-поле locale; props={props}"
    # И прежние поля на месте (обратносовместимость, нулевые регрессии).
    assert "prompt" in props


async def test_openapi_post_edits_has_no_locale_field():
    """POST /v1/projects/{project_id}/edits request-body НЕ содержит `locale` (scope §2).

    Правка наследует язык сайта через маркер `**Content language:**`; контракт /edits
    не меняется. Наличие locale здесь было бы нарушением ADR-036 §2.
    """
    from app.api.main import app

    openapi = app.openapi()
    props = _request_body_props(openapi, "/v1/projects/{project_id}/edits")
    assert "locale" not in props, (
        f"POST /edits НЕ должен принимать locale (ADR-036 §2 scope); props={props}"
    )
    # Контракт /edits на месте: instruction (+ images) присутствует.
    assert "instruction" in props


async def test_edits_ignores_locale_form_field_no_422(client, auth_headers, seeded_user):
    """Behaviour: /edits с лишним полем `locale` в форме НЕ падает 422 из-за locale —
    отрабатывает штатный путь (404 на несуществующий проект). Контракт /edits не изменился
    (locale просто не объявлен → FastAPI игнорирует лишнее form-поле).
    """
    resp = await client.post(
        "/v1/projects/p_nonexistent00000000000/edits",
        data={"instruction": "make header blue", "locale": "ru"},
        headers={**auth_headers, "Idempotency-Key": "edit-locale-key"},
    )
    # 404 (проект не существует/не свой), НЕ 422 из-за locale — поле проигнорировано.
    assert resp.status_code == 404, (
        f"/edits не должен 422 из-за лишнего locale (поле игнорируется); got {resp.status_code}: "
        f"{resp.text}"
    )
