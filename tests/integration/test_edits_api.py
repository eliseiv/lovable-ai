"""Integration: POST /v1/projects/{pid}/edits (Sprint 5, ADR-014 §A, docs/06 §S5 Edits).

Реальный Postgres (client шарит тест-сессию). ADR-034 §D11: /edits — multipart
(instruction как Form). dispatch_for_state (edit_service) и
enqueue замоканы — проверяем контракт сервиса без Celery. Покрывает:
  - non-LIVE проект (нет good-ревизии) → 409;
  - чужой/несуществующий pid → 404 (cross-tenant);
  - idempotency-replay того же Idempotency-Key → НЕ новая правка (тот же job_id, не 402,
    quota не списывается повторно);
  - edit_quota_exhausted: Free monthly_edits=5 исчерпан → 402 reason=edit_quota_exhausted;
  - Pro (monthly_edits=NULL) — безлимит, проходит;
  - Idempotency-Key обязателен → 422.

Инкремент edit_usage и реальный Agent 4 — в test_edit_pipeline (task-уровень), здесь —
контракт endpoint/гейта.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.ids import (
    new_job_id,
    new_project_id,
    new_revision_id,
    new_subscription_id,
)
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import (
    EditUsageCounter,
    GenerationJob,
    Project,
    Revision,
    Subscription,
    User,
)
from app.services import edit_service

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_edit_dispatch(monkeypatch):  # noqa: ANN001, ANN202
    """edit_service.dispatch_for_state — no-op (без Celery). Сигнатура с kind (S5)."""
    monkeypatch.setattr(edit_service, "dispatch_for_state", lambda *a, **k: None)


async def _user(session, uid, key) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(key),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def _live_project_with_good_revision(session, uid) -> str:  # noqa: ANN001
    """Проект с current good-ревизией (LIVE-семантика для /edits)."""
    pid = new_project_id()
    src_jid = new_job_id()
    rid = new_revision_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    session.add(
        GenerationJob(
            id=src_jid,
            project_id=pid,
            user_id=uid,
            state=JobState.LIVE,
            kind="generation",
            spec_tz="spec text",
            spec_ref=None,
        )
    )
    await session.flush()  # job до revision (FK created_from_job_id)
    session.add(
        Revision(
            id=rid,
            project_id=pid,
            revision_no=1,
            source_artifact_ref="s3://src/1",
            created_from_job_id=src_jid,
            is_good=True,
        )
    )
    await session.flush()
    proj = await session.get(Project, pid)
    proj.current_revision_id = rid
    await session.flush()
    return pid


async def _sub(session, uid, access_level, status="active") -> None:  # noqa: ANN001
    session.add(
        Subscription(
            id=new_subscription_id(),
            user_id=uid,
            access_level=access_level,
            status=status,
            will_renew=True,
            raw={},
            synced_at=datetime.now(UTC),
        )
    )
    await session.flush()


def _hdr(key, idem="edit-key"):  # noqa: ANN001
    return {"Authorization": f"Bearer {key}", "Idempotency-Key": idem}


# --- 409 non-LIVE ---


async def test_edit_non_live_project_409(client, session):
    uid = "u_edit_nonlive000001"
    await _user(session, uid, "edit-nonlive-key")
    # Проект без current_revision_id (не LIVE).
    pid = new_project_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    await session.flush()
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "make blue"},
        headers=_hdr("edit-nonlive-key"),
    )
    assert resp.status_code == 409


# --- 404 cross-tenant ---


async def test_edit_cross_tenant_404(client, session, other_user):
    uid = "u_edit_owner00000001"
    await _user(session, uid, "edit-owner-key")
    # Проект other_user — owner не видит.
    pid = await _live_project_with_good_revision(session, other_user.id)
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-owner-key"),
    )
    assert resp.status_code == 404


async def test_edit_unknown_project_404(client, session):
    uid = "u_edit_unknown000001"
    await _user(session, uid, "edit-unknown-key")
    resp = await client.post(
        "/v1/projects/p_doesnotexist00000001/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-unknown-key"),
    )
    assert resp.status_code == 404


# --- idempotency-replay не новая правка ---


async def test_edit_idempotency_replay_same_job_not_402(client, session):
    uid = "u_edit_idem000000001"
    await _user(session, uid, "edit-idem-key")
    pid = await _live_project_with_good_revision(session, uid)

    r1 = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "first"},
        headers=_hdr("edit-idem-key", "idem-AAA"),
    )
    assert r1.status_code == 202
    job_id = r1.json()["job_id"]

    # Повтор того же ключа → тот же job_id, НЕ 402, НЕ новая правка.
    r2 = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "first"},
        headers=_hdr("edit-idem-key", "idem-AAA"),
    )
    assert r2.status_code == 202
    assert r2.json()["job_id"] == job_id


async def test_edit_idempotency_replay_when_quota_exhausted_still_replays(client, session):
    """Replay существующего ключа резолвится ДО quota-gate → не ловит 402 даже при исчерпании."""
    uid = "u_edit_idemq00000001"
    await _user(session, uid, "edit-idemq-key")
    pid = await _live_project_with_good_revision(session, uid)

    # Первая правка проходит.
    r1 = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-idemq-key", "idem-Q"),
    )
    assert r1.status_code == 202
    job_id = r1.json()["job_id"]

    # Исчерпываем Free edit-квоту (monthly_edits=5).
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=5))
    await session.flush()

    # Replay того же ключа всё равно 202 (idempotency раньше gate).
    r2 = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-idemq-key", "idem-Q"),
    )
    assert r2.status_code == 202
    assert r2.json()["job_id"] == job_id


# --- edit_quota_exhausted (Free=5) ---


async def test_edit_quota_exhausted_free_402(client, session):
    uid = "u_edit_quota00000001"
    await _user(session, uid, "edit-quota-key")
    pid = await _live_project_with_good_revision(session, uid)
    # Free monthly_edits=5 (миграция 0006) → used=5 исчерпывает.
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=5))
    await session.flush()
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-quota-key", "idem-new"),
    )
    assert resp.status_code == 402
    body = resp.json()
    assert body["status"] == 402
    assert body["reason"] == "edit_quota_exhausted"


async def test_edit_free_under_quota_passes(client, session):
    uid = "u_edit_under00000001"
    await _user(session, uid, "edit-under-key")
    pid = await _live_project_with_good_revision(session, uid)
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=4))  # 4<5
    await session.flush()
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-under-key", "idem-under"),
    )
    assert resp.status_code == 202


# --- Pro безлимит ---


async def test_edit_pro_unlimited_passes_even_with_high_usage(client, session):
    uid = "u_edit_pro0000000001"
    await _user(session, uid, "edit-pro-key")
    await _sub(session, uid, "pro", "active")
    pid = await _live_project_with_good_revision(session, uid)
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=999))
    await session.flush()
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers=_hdr("edit-pro-key", "idem-pro"),
    )
    # Pro monthly_edits=NULL → безлимит, не гейтится.
    assert resp.status_code == 202


# --- Idempotency-Key обязателен ---


async def test_edit_missing_idempotency_key_422(client, session):
    uid = "u_edit_noidem0000001"
    await _user(session, uid, "edit-noidem-key")
    pid = await _live_project_with_good_revision(session, uid)
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "x"},
        headers={"Authorization": "Bearer edit-noidem-key"},
    )
    assert resp.status_code == 422
