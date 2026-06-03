"""Integration: notify.apns_push — выбор устройств владельца + side-effects (ADR-013).

Реальный Postgres (session_scope autonomous_db); внешний APNs HTTP/2 (ApnsClient.send)
мокается. Покрывает (docs/06 §S5 APNs integration):
  - no-op без credentials (apns_configured == False): send не вызывается, пайплайн цел;
  - push выбирает ТОЛЬКО устройства владельца джобы (cross-tenant: чужие не получают);
  - 200 → last_push_at проставлен; 410/invalid_token → invalidated_at проставлен;
  - инвалидированное устройство не выбирается (active_devices фильтрует);
  - промежуточный to_state (BUILDING) → нет отправки (should_push=False).

Запуск тела _apns_push напрямую (не через Celery-обёртку) — детерминизм без брокера.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.ids import new_device_token_id, new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import DeviceToken, GenerationJob, JobEvent, Project, User
from app.db.session import session_scope
from app.notify import tasks as notify_tasks
from app.notify.apns_client import ApnsResult

pytestmark = pytest.mark.asyncio

OWNER = "u_apnsowner0000000001"
OTHER = "u_apnsother0000000001"


async def _purge() -> None:
    async with session_scope() as s:
        for uid in (OWNER, OTHER):
            job_ids = (
                (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == uid)))
                .scalars()
                .all()
            )
            if job_ids:
                await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
            await s.execute(delete(DeviceToken).where(DeviceToken.user_id == uid))
            await s.execute(delete(Project).where(Project.user_id == uid))
            await s.execute(delete(User).where(User.id == uid))
        await s.commit()


async def _seed_job_with_devices() -> str:
    """Owner-джоба LIVE + 1 активное устройство owner + 1 устройство other (cross-tenant)."""
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        for uid in (OWNER, OTHER):
            s.add(
                User(
                    id=uid,
                    api_key_hash=hash_api_key(f"{uid}-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
        await s.flush()  # users до FK-зависимых строк (device_tokens/jobs)
        s.add(Project(id=pid, user_id=OWNER, prompt="x", title=None))
        s.add(
            GenerationJob(
                id=jid, project_id=pid, user_id=OWNER, state=JobState.LIVE, kind="generation"
            )
        )
        s.add(
            DeviceToken(
                id=new_device_token_id(),
                user_id=OWNER,
                apns_token="owner-device-tok",
                platform="ios",
                environment="sandbox",
            )
        )
        s.add(
            DeviceToken(
                id=new_device_token_id(),
                user_id=OTHER,
                apns_token="other-device-tok",
                platform="ios",
                environment="sandbox",
            )
        )
        await s.commit()
    return jid


@pytest_asyncio.fixture
async def push_env(autonomous_db):  # noqa: ANN001, ANN201
    await _purge()
    jid = await _seed_job_with_devices()
    yield jid
    await _purge()


class _SpyClient:
    """Спай ApnsClient: фиксирует отправленные токены, возвращает заранее заданный результат."""

    def __init__(self, settings, result: ApnsResult) -> None:  # noqa: ANN001
        self._result = result
        self.sent: list[str] = []

    async def send(self, *, apns_token, device_environment, payload):  # noqa: ANN001, ANN003
        self.sent.append(apns_token)
        return self._result


def _ok() -> ApnsResult:
    return ApnsResult(ok=True, invalid_token=False, status_code=200, detail="ok")


async def test_no_op_without_credentials(push_env, monkeypatch):
    """apns_configured == False (env conftest) → send не вызывается (no-op, пайплайн цел)."""
    called = {"send": False}

    def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        called["send"] = True
        raise AssertionError("ApnsClient must not be constructed when unconfigured")

    monkeypatch.setattr(notify_tasks, "ApnsClient", _boom)
    # Сразу выходит на apns_configured-чеке (settings из env: ключей нет).
    await notify_tasks._apns_push(push_env, "LIVE")
    assert called["send"] is False


async def test_push_only_owner_devices(push_env, apns_credentials, monkeypatch):
    spy_holder = {}

    def _make_client(settings):  # noqa: ANN001, ANN202
        spy = _SpyClient(settings, _ok())
        spy_holder["spy"] = spy
        return spy

    monkeypatch.setattr(notify_tasks, "ApnsClient", _make_client)
    await notify_tasks._apns_push(push_env, "LIVE")
    # Cross-tenant: отправка строго на устройство владельца, чужое не выбрано.
    assert spy_holder["spy"].sent == ["owner-device-tok"]


async def test_push_200_sets_last_push_at(push_env, apns_credentials, monkeypatch):
    monkeypatch.setattr(
        notify_tasks,
        "ApnsClient",
        lambda s: _SpyClient(s, _ok()),
    )
    await notify_tasks._apns_push(push_env, "FAILED")
    async with session_scope() as s:
        row = (
            await s.execute(select(DeviceToken).where(DeviceToken.apns_token == "owner-device-tok"))
        ).scalar_one()
        assert row.last_push_at is not None


async def test_push_invalid_token_sets_invalidated_at(push_env, apns_credentials, monkeypatch):
    monkeypatch.setattr(
        notify_tasks,
        "ApnsClient",
        lambda s: _SpyClient(
            s, ApnsResult(ok=False, invalid_token=True, status_code=410, detail="Unregistered")
        ),
    )
    await notify_tasks._apns_push(push_env, "LIVE")
    async with session_scope() as s:
        row = (
            await s.execute(select(DeviceToken).where(DeviceToken.apns_token == "owner-device-tok"))
        ).scalar_one()
        assert row.invalidated_at is not None


async def test_invalidated_device_not_pushed(push_env, apns_credentials, monkeypatch):
    # Помечаем единственное owner-устройство мёртвым → push не должен никого выбрать.
    async with session_scope() as s:
        row = (
            await s.execute(select(DeviceToken).where(DeviceToken.apns_token == "owner-device-tok"))
        ).scalar_one()
        row.invalidated_at = datetime.now(UTC)
        await s.commit()

    spy_holder = {}
    monkeypatch.setattr(
        notify_tasks,
        "ApnsClient",
        lambda s: spy_holder.setdefault("spy", _SpyClient(s, _ok())),
    )
    await notify_tasks._apns_push(push_env, "LIVE")
    # ApnsClient может вообще не строиться (нет активных устройств) — главное, что ничего не ушло.
    assert spy_holder.get("spy") is None or spy_holder["spy"].sent == []


async def test_intermediate_state_no_push(push_env, apns_credentials, monkeypatch):
    def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("no push for intermediate state")

    monkeypatch.setattr(notify_tasks, "ApnsClient", _boom)
    await notify_tasks._apns_push(push_env, "BUILDING")  # should_push=False → ранний выход
