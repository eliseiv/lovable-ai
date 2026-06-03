"""Integration (Sprint 6, TD-008/TD-009, ADR-016): закрытие scale-долга.

Реальный Postgres (conftest). Покрывает:
  - N+1 list_projects (TD-008): get_live_urls_for_projects по N проектам → РОВНО 1 SQL
    (счётчик выполненных SELECT через SQLAlchemy event), результат корректен;
  - billing.resync батч+курсор (TD-009): .limit(BATCH) ограничивает число обработанных,
    ORDER BY synced_at ASC (самые протухшие первыми), метрика billing_resync_batch{full}
    при заполненном батче (есть хвост) и {partial} когда всё влезло.

Внешняя граница Adapty (getProfile) мокается; Postgres — реальный.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.ids import new_deployment_id, new_job_id, new_project_id, new_revision_id
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Revision, SiteDeployment, Subscription
from app.services import project_service

pytestmark = pytest.mark.asyncio


async def _make_live_project(session, user_id: str, idx: int) -> str:  # noqa: ANN001
    pid = new_project_id()
    jid = new_job_id()
    rid = new_revision_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title=f"P{idx}"))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=user_id,
            state=JobState.LIVE,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()
    session.add(
        Revision(
            id=rid,
            project_id=pid,
            revision_no=1,
            source_artifact_ref=f"sources/{idx}/source.tgz",
            created_from_job_id=jid,
            is_good=True,
        )
    )
    await session.flush()
    sub = f"{idx:016x}"[:16]
    session.add(
        SiteDeployment(
            id=new_deployment_id(),
            project_id=pid,
            revision_id=rid,
            subdomain=sub,
            live_url=f"http://{sub}.apps.localhost/",
            dist_artifact_ref=f"dist/{idx}/dist.tgz",
            status="active",
        )
    )
    await session.flush()
    return pid


async def test_list_projects_live_url_single_query_no_n_plus_1(session, seeded_user):  # noqa: ANN001
    """TD-008: live_url для N проектов берётся ОДНИМ SELECT (не N+1)."""
    pids = [await _make_live_project(session, seeded_user.id, i) for i in range(5)]

    # Считаем SELECT'ы к site_deployments во время батч-запроса live_url.
    select_count = {"n": 0}

    @event.listens_for(session.sync_session, "do_orm_execute")
    def _count(orm_execute_state):  # noqa: ANN001, ANN202
        if orm_execute_state.is_select:
            stmt = str(orm_execute_state.statement)
            if "site_deployments" in stmt:
                select_count["n"] += 1

    live = await project_service.get_live_urls_for_projects(session, pids)

    assert select_count["n"] == 1, "live_url по N проектам должен быть ОДНИМ запросом (TD-008)"
    assert len(live) == 5
    for pid in pids:
        assert pid in live
        assert live[pid].endswith(".apps.localhost/")


async def test_get_live_urls_empty_input_no_query(session, seeded_user):  # noqa: ANN001
    """Пустой список project_id → пустой результат без обращения к БД."""
    select_count = {"n": 0}

    @event.listens_for(session.sync_session, "do_orm_execute")
    def _count(orm_execute_state):  # noqa: ANN001, ANN202
        if orm_execute_state.is_select:
            select_count["n"] += 1

    assert await project_service.get_live_urls_for_projects(session, []) == {}
    assert select_count["n"] == 0


# --- TD-009: billing.resync батч + курсор ---


class _FakeAdaptyClient:
    """Мок Adapty getProfile: профиль не найден (resync помечает synced_at=now, апдейта нет)."""

    async def get_profile(self, user_id: str):  # noqa: ANN201
        return None


async def _make_stale_sub(session, user_id: str, age_s: int) -> None:  # noqa: ANN001
    """Подписка с synced_at в прошлом (протухшая) для попадания в resync-батч."""
    from app.core.ids import new_subscription_id
    from app.db.models import User

    session.add(User(id=user_id, api_key_hash=f"h_{user_id}", monthly_budget_usd=Decimal("50")))
    await session.flush()
    session.add(
        Subscription(
            id=new_subscription_id(),
            user_id=user_id,
            access_level="pro",
            status="active",
            will_renew=True,
            raw={},
            synced_at=datetime.now(UTC) - timedelta(seconds=age_s),
        )
    )
    await session.flush()


async def test_resync_batch_limit_and_cursor_oldest_first(session, monkeypatch):  # noqa: ANN001
    """TD-009: .limit(BATCH) ограничивает батч; ORDER BY synced_at ASC (самые протухшие первыми)."""
    from app.billing import resync
    from app.core.config import get_settings

    settings = get_settings()
    interval = settings.billing_resync_interval_s
    # 4 протухших подписки разного возраста; урезаем батч до 2.
    for i, age in enumerate((interval + 4000, interval + 3000, interval + 2000, interval + 1000)):
        await _make_stale_sub(session, f"u_resync_{i}", age)
    monkeypatch.setattr(settings, "billing_resync_batch_size", 2, raising=False)

    # Перехватываем, какие user_id реально ресинкаются (по порядку курсора).
    resynced_order: list[str] = []
    orig = resync.resync_user

    async def _spy(session_, *, user_id, sub, client):  # noqa: ANN001, ANN002, ANN003
        resynced_order.append(user_id)
        return await orig(session_, user_id=user_id, sub=sub, client=client)

    monkeypatch.setattr(resync, "resync_user", _spy)

    await resync.run_periodic_resync(session, _FakeAdaptyClient())

    # Батч ограничен 2 (TD-009) — обработаны только самые протухшие.
    assert len(resynced_order) == 2
    # Курсор synced_at ASC: u_resync_0 (age +4000, самый протухший) и u_resync_1 (+3000).
    assert resynced_order == ["u_resync_0", "u_resync_1"]


async def test_resync_batch_metric_full_when_batch_filled(session, monkeypatch):  # noqa: ANN001
    """billing_resync_batch{result=full} при заполненном батче (есть хвост на след. тик)."""
    from prometheus_client import REGISTRY

    from app.billing import resync
    from app.core.config import get_settings

    settings = get_settings()
    interval = settings.billing_resync_interval_s
    for i in range(3):
        await _make_stale_sub(session, f"u_full_{i}", interval + 1000 + i)
    monkeypatch.setattr(settings, "billing_resync_batch_size", 2, raising=False)

    before = (
        REGISTRY.get_sample_value("lovable_billing_resync_batch_count", {"result": "full"}) or 0.0
    )
    await resync.run_periodic_resync(session, _FakeAdaptyClient())
    after = (
        REGISTRY.get_sample_value("lovable_billing_resync_batch_count", {"result": "full"}) or 0.0
    )
    # Батч заполнен под лимит (2 из 3) → full (хвост на следующем тике).
    assert after == before + 1


async def test_resync_batch_metric_partial_when_all_fit(session, monkeypatch):  # noqa: ANN001
    """billing_resync_batch{result=partial} когда все протухшие влезли в батч."""
    from prometheus_client import REGISTRY

    from app.billing import resync
    from app.core.config import get_settings

    settings = get_settings()
    interval = settings.billing_resync_interval_s
    await _make_stale_sub(session, "u_partial_0", interval + 1000)
    monkeypatch.setattr(settings, "billing_resync_batch_size", 50, raising=False)

    before = (
        REGISTRY.get_sample_value("lovable_billing_resync_batch_count", {"result": "partial"})
        or 0.0
    )
    await resync.run_periodic_resync(session, _FakeAdaptyClient())
    after = (
        REGISTRY.get_sample_value("lovable_billing_resync_batch_count", {"result": "partial"})
        or 0.0
    )
    assert after == before + 1
