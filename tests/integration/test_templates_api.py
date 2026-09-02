"""Integration: каталог шаблонов и старт генерации по шаблону (ADR-048).

Покрытие:
  - `GET /templates` — карточки в порядке каталога, `preview_url` = `null` без картинки
    и абсолютный URL на этом же домене, когда файл в образе есть;
  - `GET /templates/{id}/preview` — картинка/404, гейт авторизации;
  - `POST /projects` с `template_id` — стартует обычная генерация с промптом шаблона;
    пользовательский текст добавляется как уточнение, не заменяя шаблон;
  - неизвестный `template_id` → 422; отсутствие и промпта, и шаблона → 422;
  - обратная совместимость: свободный промпт без `template_id` работает как раньше.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.security import hash_api_key
from app.db.models import Project, User
from app.services import template_service

pytestmark = pytest.mark.asyncio

_UID = "u_templates00001"


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


async def test_catalog_lists_templates_in_order(client, session):
    await _user(session)

    resp = await client.get("/v1/templates", headers=_auth())
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [t.id for t in template_service.list_templates()]
    assert all(i["title"] for i in items)


async def test_preview_url_is_null_without_image(client, session, monkeypatch):
    await _user(session)
    monkeypatch.setattr(template_service, "preview_path", lambda template_id: None)

    resp = await client.get("/v1/templates", headers=_auth())
    assert all(item["preview_url"] is None for item in resp.json()["items"])


async def test_preview_url_points_to_this_instance(client, session, monkeypatch, tmp_path):
    await _user(session)
    image = tmp_path / "landing-page.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    monkeypatch.setattr(
        template_service,
        "preview_path",
        lambda template_id: image if template_id == "landing-page" else None,
    )

    items = {
        i["id"]: i for i in (await client.get("/v1/templates", headers=_auth())).json()["items"]
    }
    assert items["landing-page"]["preview_url"].endswith("/v1/templates/landing-page/preview")
    assert items["online-shop"]["preview_url"] is None

    preview = await client.get("/v1/templates/landing-page/preview", headers=_auth())
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert preview.content == b"\xff\xd8\xff\xe0jpeg-bytes"


async def test_preview_missing_is_404(client, session):
    await _user(session)

    assert (await client.get("/v1/templates/no-such/preview", headers=_auth())).status_code == 404


async def test_templates_require_auth(client):
    assert (await client.get("/v1/templates")).status_code == 401


async def test_create_project_from_template_uses_its_prompt(client, session):
    await _user(session)
    template = template_service.get_template("online-shop")
    assert template is not None

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "tpl-1"},
        data={"template_id": "online-shop", "locale": "ru"},
    )
    assert resp.status_code == 202
    project = await session.get(Project, resp.json()["project_id"])
    assert project is not None
    assert project.prompt == template.prompt
    assert project.requested_locale == "ru"


async def test_user_text_refines_template_prompt(client, session):
    await _user(session)
    template = template_service.get_template("medical")
    assert template is not None

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "tpl-2"},
        data={"template_id": "medical", "prompt": "стоматология в Казани"},
    )
    assert resp.status_code == 202
    project = await session.get(Project, resp.json()["project_id"])
    assert project is not None
    assert project.prompt.startswith(template.prompt)
    assert "стоматология в Казани" in project.prompt


async def test_unknown_template_is_422(client, session):
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "tpl-3"},
        data={"template_id": "does-not-exist"},
    )
    assert resp.status_code == 422


async def test_neither_prompt_nor_template_is_422(client, session):
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "tpl-4"},
        data={"title": "без промпта"},
    )
    assert resp.status_code == 422


async def test_plain_prompt_still_works(client, session):
    """Обратная совместимость: свободный промпт без template_id — прежний путь."""
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "tpl-5"},
        data={"prompt": "лендинг для кофейни"},
    )
    assert resp.status_code == 202
    project = await session.get(Project, resp.json()["project_id"])
    assert project is not None
    assert project.prompt == "лендинг для кофейни"
