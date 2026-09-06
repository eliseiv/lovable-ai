"""Integration: каталог стилей и генерация с выбранным стилем (ADR-050).

Покрытие:
  - `GET /styles` — две карточки в порядке каталога, у каждой превью в образе;
  - `GET /styles/{id}/preview` — JPEG/404, гейт авторизации;
  - `POST /projects` со `style_id`: стиль дописывается к промпту и сочетается с шаблоном
    и текстом пользователя; порядок частей — задание → стиль → уточнение;
  - стиль сам по себе сайт не описывает: `style_id` без `prompt`/`template_id` → 422;
  - неизвестный `style_id` → 422; прежние сочетания без стиля не сломаны.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.security import hash_api_key
from app.db.models import Project, User
from app.services import style_service, template_service
from app.services.prompt_composer import STYLE_PREFIX, USER_PREFIX

pytestmark = pytest.mark.asyncio

_UID = "u_styles000000001"


async def _user(session) -> None:  # noqa: ANN001
    session.add(
        User(
            id=_UID,
            api_key_hash=hash_api_key(f"{_UID}-legacy-key"),
            monthly_budget_usd=Decimal("50.0000"),
            status="active",
        )
    )
    await session.flush()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_UID}-legacy-key"}


async def _created_prompt(client, session, data: dict[str, str], key: str) -> str:  # noqa: ANN001
    resp = await client.post("/v1/projects", headers={**_auth(), "Idempotency-Key": key}, data=data)
    assert resp.status_code == 202, resp.text
    project = await session.get(Project, resp.json()["project_id"])
    assert project is not None
    return project.prompt


# ============================ каталог ============================


async def test_catalog_lists_two_styles_with_previews(client, session):
    await _user(session)

    items = (await client.get("/v1/styles", headers=_auth())).json()["items"]
    assert [i["id"] for i in items] == ["mui", "human-interface"]
    assert [i["title"] for i in items] == ["MUI", "Human Interface"]
    assert all(i["preview_url"].endswith(f"/v1/styles/{i['id']}/preview") for i in items)

    for item in items:
        preview = await client.get(f"/v1/styles/{item['id']}/preview", headers=_auth())
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/jpeg"
        assert preview.content[:2] == b"\xff\xd8"  # JPEG SOI: отдаётся картинка


async def test_preview_url_is_null_without_image(client, session, monkeypatch):
    await _user(session)
    monkeypatch.setattr(style_service, "preview_path", lambda style_id: None)

    items = (await client.get("/v1/styles", headers=_auth())).json()["items"]
    assert all(item["preview_url"] is None for item in items)


async def test_unknown_style_preview_is_404(client, session):
    await _user(session)

    assert (await client.get("/v1/styles/no-such/preview", headers=_auth())).status_code == 404


async def test_styles_require_auth(client):
    assert (await client.get("/v1/styles")).status_code == 401


# ============================ генерация со стилем ============================


async def test_style_appends_directive_to_free_prompt(client, session):
    await _user(session)
    style = style_service.get_style("mui")
    assert style is not None

    prompt = await _created_prompt(
        client, session, {"prompt": "лендинг для кофейни", "style_id": "mui"}, "st-1"
    )
    assert prompt.startswith("лендинг для кофейни")
    assert f"{STYLE_PREFIX}{style.prompt}" in prompt


async def test_template_style_and_user_text_compose_in_order(client, session):
    """Порядок частей: задание шаблона → стиль → уточнение пользователя."""
    await _user(session)
    template = template_service.get_template("online-shop")
    style = style_service.get_style("human-interface")
    assert template is not None and style is not None

    prompt = await _created_prompt(
        client,
        session,
        {
            "template_id": "online-shop",
            "style_id": "human-interface",
            "prompt": "магазин кофе",
            "locale": "ru",
        },
        "st-2",
    )
    assert prompt.startswith(template.prompt)
    assert prompt.index(STYLE_PREFIX) > prompt.index(template.prompt)
    assert prompt.index(USER_PREFIX) > prompt.index(STYLE_PREFIX)
    assert prompt.endswith("магазин кофе")


async def test_human_interface_directive_forbids_ios_mockup(client, session):
    """Директива Apple-стиля прямо запрещает рисовать копию iOS-приложения (ADR-050 §B)."""
    style = style_service.get_style("human-interface")
    assert style is not None
    assert "RESPONSIVE WEBSITE" in style.prompt
    assert "SwiftUI" in style.prompt


async def test_style_alone_is_422(client, session):
    """Стиль описывает оформление, а не сайт: без промпта и шаблона генерировать нечего."""
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "st-3"},
        data={"style_id": "mui"},
    )
    assert resp.status_code == 422


async def test_unknown_style_id_is_422(client, session):
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "st-4"},
        data={"prompt": "лендинг", "style_id": "does-not-exist"},
    )
    assert resp.status_code == 422


async def test_template_without_style_unchanged(client, session):
    """Обратная совместимость: без style_id промпт шаблона идёт как раньше, без директивы."""
    await _user(session)
    template = template_service.get_template("medical")
    assert template is not None

    prompt = await _created_prompt(client, session, {"template_id": "medical"}, "st-5")
    assert prompt == template.prompt
    assert STYLE_PREFIX not in prompt
