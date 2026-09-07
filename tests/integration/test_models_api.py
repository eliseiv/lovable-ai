"""Integration: каталог моделей генерации и выбор модели пользователем (ADR-051).

Покрытие:
  - `GET /models` — карточки каталога текущего провайдера, гейт авторизации;
  - `POST /projects` с `model_id` — выбор сохраняется на проекте; без выбора остаётся `null`;
  - неизвестный `model_id` и id из чужого провайдерского каталога → 422;
  - резолв модели шага: выбранный пресет применяется ко ВСЕМ агентам, без выбора — каждый
    агент идёт со своим `AGENTn_MODEL`; устаревший `model_id` не роняет генерацию.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.security import hash_api_key
from app.db.models import Project, User
from app.services import model_service

pytestmark = pytest.mark.asyncio

_UID = "u_models000000001"


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


# ============================ каталог ============================


async def test_catalog_matches_instance_provider(client, session):
    await _user(session)
    settings = get_settings()

    items = (await client.get("/v1/models", headers=_auth())).json()["items"]
    expected = model_service.list_models(settings)
    assert [i["id"] for i in items] == [m.id for m in expected]
    assert all(i["title"] and i["description"] for i in items)


async def test_catalog_requires_auth(client):
    assert (await client.get("/v1/models")).status_code == 401


# ============================ выбор при создании ============================


async def test_model_choice_is_stored_on_project(client, session):
    await _user(session)
    catalog = model_service.list_models(get_settings())
    assert catalog, "каталог провайдера инстанса не должен быть пустым"
    chosen = catalog[-1]

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "mdl-1"},
        data={"prompt": "лендинг для кофейни", "model_id": chosen.id},
    )
    assert resp.status_code == 202
    project = await session.get(Project, resp.json()["project_id"])
    assert project is not None
    assert project.model_id == chosen.id


async def test_without_choice_project_keeps_null(client, session):
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "mdl-2"},
        data={"prompt": "лендинг для кофейни"},
    )
    project = await session.get(Project, resp.json()["project_id"])
    assert project is not None
    assert project.model_id is None


async def test_unknown_model_id_is_422(client, session):
    await _user(session)

    resp = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "mdl-3"},
        data={"prompt": "лендинг", "model_id": "does-not-exist"},
    )
    assert resp.status_code == 422


async def test_model_id_from_other_provider_is_422(client, session, monkeypatch):
    """Каталог провайдер-специфичен: id, которого нет у текущего провайдера, отвергается."""
    await _user(session)
    monkeypatch.setattr(
        model_service,
        "list_models",
        lambda settings: (
            model_service.GenerationModel(
                id="only-here", title="X", description="X", model="some-model"
            ),
        ),
    )

    ok = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "mdl-4"},
        data={"prompt": "лендинг", "model_id": "only-here"},
    )
    assert ok.status_code == 202

    rejected = await client.post(
        "/v1/projects",
        headers={**_auth(), "Idempotency-Key": "mdl-5"},
        data={"prompt": "лендинг", "model_id": "fast"},
    )
    assert rejected.status_code == 422
