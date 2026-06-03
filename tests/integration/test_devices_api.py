"""Integration: POST/DELETE /v1/devices (Sprint 5, ADR-013, docs/modules/api §/devices).

Реальный Postgres (client шарит тест-сессию). Покрывает:
  - POST /devices → 201 + id; upsert идемпотентен (повтор того же токена → та же строка,
    invalidated_at сброшен);
  - невалидный platform/environment → 422;
  - DELETE /devices/{token} → 204 (invalidated_at=now); повтор → 204 (идемпотентно по сути)
    — но контракт: токен уже invalidated всё ещё «найден» → 204; реально удалённого нет → 404;
  - cross-tenant: чужой токен → DELETE 404 (не раскрываем существование);
  - после DELETE токен не попадает в active_devices_for_user (push его не выберет).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import DeviceToken
from app.notify import device_service

pytestmark = pytest.mark.asyncio


def _hdr(extra: dict | None = None) -> dict[str, str]:
    h = {"Authorization": "Bearer qa-test-bearer-key"}
    if extra:
        h.update(extra)
    return h


async def test_post_device_registers_201(client, seeded_user):
    resp = await client.post(
        "/v1/devices",
        json={"apns_token": "tok-aaa", "platform": "ios", "environment": "sandbox"},
        headers=_hdr(),
    )
    assert resp.status_code == 201
    assert resp.json()["id"].startswith("dev_")


async def test_post_device_upsert_idempotent_same_row(client, session, seeded_user):
    body = {"apns_token": "tok-dup", "platform": "ios", "environment": "sandbox"}
    r1 = await client.post("/v1/devices", json=body, headers=_hdr())
    r2 = await client.post("/v1/devices", json=body, headers=_hdr())
    assert r1.status_code == r2.status_code == 201
    # Upsert по (user_id, apns_token): ровно одна строка.
    rows = (
        (
            await session.execute(
                select(DeviceToken).where(
                    DeviceToken.user_id == seeded_user.id, DeviceToken.apns_token == "tok-dup"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_post_device_reactivates_invalidated(client, session, seeded_user):
    body = {"apns_token": "tok-react", "platform": "ios", "environment": "sandbox"}
    await client.post("/v1/devices", json=body, headers=_hdr())
    # Инвалидируем (как APNs 410) и пере-регистрируем — invalidated_at должен сброситься.
    await client.delete("/v1/devices/tok-react", headers=_hdr())
    await client.post("/v1/devices", json=body, headers=_hdr())
    row = (
        await session.execute(select(DeviceToken).where(DeviceToken.apns_token == "tok-react"))
    ).scalar_one()
    assert row.invalidated_at is None


@pytest.mark.parametrize(
    "body",
    [
        {"apns_token": "t", "platform": "android", "environment": "sandbox"},
        {"apns_token": "t", "platform": "ios", "environment": "prod"},
        {"apns_token": "t", "platform": "ios", "environment": "bogus"},
    ],
)
async def test_post_device_invalid_platform_or_env_422(client, seeded_user, body):
    resp = await client.post("/v1/devices", json=body, headers=_hdr())
    assert resp.status_code == 422


async def test_post_device_missing_token_422(client, seeded_user):
    # apns_token min_length=1 (Pydantic) → пустой не проходит валидацию схемы.
    resp = await client.post(
        "/v1/devices",
        json={"apns_token": "", "platform": "ios", "environment": "sandbox"},
        headers=_hdr(),
    )
    assert resp.status_code == 422


async def test_delete_device_204(client, session, seeded_user):
    await client.post(
        "/v1/devices",
        json={"apns_token": "tok-del", "platform": "ios", "environment": "sandbox"},
        headers=_hdr(),
    )
    resp = await client.delete("/v1/devices/tok-del", headers=_hdr())
    assert resp.status_code == 204
    # Инвалидирован → не активен (push не выберет).
    active = await device_service.active_devices_for_user(session, seeded_user.id)
    assert all(d.apns_token != "tok-del" for d in active)


async def test_delete_unknown_device_404(client, seeded_user):
    resp = await client.delete("/v1/devices/nonexistent-token", headers=_hdr())
    assert resp.status_code == 404


async def test_delete_cross_tenant_device_404(client, session, seeded_user, other_user):
    # Токен принадлежит other_user — seeded_user не должен его видеть/удалять.
    session.add(
        DeviceToken(
            id="dev_otheruser0000000001",
            user_id=other_user.id,
            apns_token="other-tok",
            platform="ios",
            environment="sandbox",
        )
    )
    await session.flush()
    resp = await client.delete("/v1/devices/other-tok", headers=_hdr())
    assert resp.status_code == 404
    # Строка не тронута (всё ещё активна у владельца).
    row = (
        await session.execute(select(DeviceToken).where(DeviceToken.apns_token == "other-tok"))
    ).scalar_one()
    assert row.invalidated_at is None
