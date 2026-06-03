"""Integration: grace-sweep сайтов (docs/billing/03 §6, ADR-009 §C).

grace_until < now → teardown всех active-сайтов пользователя (мок deploy.teardown_container)
→ status=expired; renew в grace отменяет (sweep не выберет active); идемпотентность повтора
(уже-expired → no-op). Только active-деплои гасятся (building/failed/superseded не трогаются).

Sweeper использует session_scope (собственная транзакция) → данные коммитятся в реальную БД
и чистятся в teardown. Docker-граница (teardown_container) изолирована моком.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.billing import subscription_sweeper
from app.core.ids import new_deployment_id, new_project_id, new_revision_id, new_subscription_id
from app.db.models import Project, Revision, SiteDeployment, Subscription, User
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio

_UID = "u_sweep0000000000000001"


async def _purge() -> None:
    from app.db.models import GenerationJob, JobEvent

    async with session_scope() as s:
        pids = (await s.execute(select(Project.id).where(Project.user_id == _UID))).scalars().all()
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == _UID)))
            .scalars()
            .all()
        )
        if pids:
            for pid in pids:
                proj = await s.get(Project, pid)
                if proj is not None:
                    proj.current_revision_id = None
            await s.flush()
            await s.execute(delete(SiteDeployment).where(SiteDeployment.project_id.in_(pids)))
            await s.execute(delete(Revision).where(Revision.project_id.in_(pids)))
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            await s.execute(delete(GenerationJob).where(GenerationJob.id.in_(job_ids)))
        if pids:
            await s.execute(delete(Project).where(Project.id.in_(pids)))
        await s.execute(delete(Subscription).where(Subscription.user_id == _UID))
        await s.execute(delete(User).where(User.id == _UID))
        await s.commit()


@pytest_asyncio.fixture
async def seeded(autonomous_db):  # noqa: ANN001, ANN201
    await _purge()
    yield
    await _purge()


async def _seed_with_deploy(*, status, grace_until, deploy_status="active"):  # noqa: ANN001
    """user+subscription+project+job+revision+site_deployment (FK-полная цепочка)."""
    async with session_scope() as s:
        s.add(
            User(id=_UID, api_key_hash=None, monthly_budget_usd=Decimal("50.0000"), status="active")
        )
        # Flush user перед FK-зависимыми вставками (нет mapped-relationship → UoW не знает порядок).
        await s.flush()
        s.add(
            Subscription(
                id=new_subscription_id(),
                user_id=_UID,
                access_level="pro",
                status=status,
                will_renew=False,
                grace_until=grace_until,
                raw={},
                synced_at=datetime.now(UTC),
            )
        )
        pid = new_project_id()
        s.add(Project(id=pid, user_id=_UID, prompt="p", title=None))
        await s.flush()
        # Revision требует created_from_job_id → создаём минимальную джобу.
        from app.core.ids import new_job_id
        from app.db.enums import JobState
        from app.db.models import GenerationJob

        jid = new_job_id()
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=_UID,
                state=JobState.LIVE,
                kind="generation",
                budget_usd=Decimal("5.0000"),
            )
        )
        await s.flush()
        rid = new_revision_id()
        s.add(
            Revision(
                id=rid,
                project_id=pid,
                revision_no=1,
                source_artifact_ref="s3://src",
                created_from_job_id=jid,
                is_good=True,
            )
        )
        await s.flush()
        did = new_deployment_id()
        s.add(
            SiteDeployment(
                id=did,
                project_id=pid,
                revision_id=rid,
                subdomain="sweepsub00000001",
                live_url="http://sweepsub00000001.apps.localhost/",
                dist_artifact_ref="s3://dist",
                status=deploy_status,
            )
        )
        await s.commit()
    return pid, did


def _patch_teardown(monkeypatch):  # noqa: ANN001
    calls: list[str] = []
    monkeypatch.setattr(
        subscription_sweeper.docker_deploy, "teardown_container", lambda name: calls.append(name)
    )
    return calls


async def test_grace_expired_tears_down_active_sites_and_sets_expired(seeded, monkeypatch):
    past = datetime.now(UTC) - timedelta(hours=1)
    pid, did = await _seed_with_deploy(status="grace", grace_until=past, deploy_status="active")
    calls = _patch_teardown(monkeypatch)

    swept = await subscription_sweeper._sweep_subscriptions()
    assert swept == 1
    assert calls == ["site_sweepsub00000001"]  # teardown реального active-сайта
    async with session_scope() as s:
        sub = (
            await s.execute(select(Subscription).where(Subscription.user_id == _UID))
        ).scalar_one()
        assert sub.status == "expired"
        assert sub.grace_until is None
        dep = await s.get(SiteDeployment, did)
        assert dep.status == "superseded"  # ресурс снят, строка сохранена для аудита


async def test_renew_in_grace_cancels_sweep(seeded, monkeypatch):
    """status=active (renew отменил grace) → sweep не выбирает, teardown не зовётся."""
    # grace_until=NULL и статус уже active (renew успел) → sweep не выбирает.
    await _seed_with_deploy(status="active", grace_until=None, deploy_status="active")
    calls = _patch_teardown(monkeypatch)

    swept = await subscription_sweeper._sweep_subscriptions()
    assert swept == 0
    assert calls == []
    async with session_scope() as s:
        dep = await s.execute(select(SiteDeployment).where(SiteDeployment.project_id.isnot(None)))
        # Active-деплой остался нетронут.
        deps = list(dep.scalars().all())
        assert any(d.status == "active" for d in deps)


async def test_grace_not_yet_expired_not_swept(seeded, monkeypatch):
    future = datetime.now(UTC) + timedelta(days=3)
    await _seed_with_deploy(status="grace", grace_until=future, deploy_status="active")
    calls = _patch_teardown(monkeypatch)
    swept = await subscription_sweeper._sweep_subscriptions()
    assert swept == 0
    assert calls == []


async def test_repeated_sweep_is_idempotent(seeded, monkeypatch):
    past = datetime.now(UTC) - timedelta(hours=1)
    await _seed_with_deploy(status="grace", grace_until=past, deploy_status="active")
    calls = _patch_teardown(monkeypatch)

    first = await subscription_sweeper._sweep_subscriptions()
    assert first == 1
    # Повторный sweep уже-expired → no-op (нет grace-строк / нет active-деплоев).
    second = await subscription_sweeper._sweep_subscriptions()
    assert second == 0
    assert calls == ["site_sweepsub00000001"]  # teardown вызван ровно один раз


async def test_non_active_deploys_not_torn_down(seeded, monkeypatch):
    """building/superseded деплои не гасятся — только реально active."""
    past = datetime.now(UTC) - timedelta(hours=1)
    await _seed_with_deploy(status="grace", grace_until=past, deploy_status="superseded")
    calls = _patch_teardown(monkeypatch)
    swept = await subscription_sweeper._sweep_subscriptions()
    # Подписка свипнута (grace отработан), но teardown НЕ звал (нет active-сайтов).
    assert swept == 1
    assert calls == []
