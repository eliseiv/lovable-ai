"""Integration: DELETE /v1/projects/{pid} soft-delete + фильтры + race-guard (S4, ADR-011).

Покрывает (docs/modules/deploy/03-architecture.md §6, ADR-011 §A/C/D,
docs/06-testing-strategy.md S4):
  - DELETE → 202 (status=deleting) + soft-delete (deleted_at выставлен) для владельца;
  - проект исчезает из GET /projects и GET /projects/{pid}→404 после DELETE;
  - cross-tenant: чужой/несуществующий pid → 404 (не раскрываем существование);
  - идемпотентность: повторный DELETE soft-deleted → 202 no-op БЕЗ перетирания deleted_at;
    физически удалённый (нет строки) → 404;
  - soft-delete-фильтры: count_projects (max_projects gate) и /billing/me.projects_used
    исключают deleted_at; quota-gate max_projects не считает удаляемый слот;
  - race-guard: tasks._deploy на soft-deleted проекте → FAILED(project_deleted), не деплоит.

Внешняя граница project.gc (Celery) изолирована: _dispatch_project_gc мокается no-op —
проверяем именно soft-delete-контракт endpoint'а, а полный GC — в test_project_gc.
client/session — общая rollback-транзакция (изоляция); race-guard использует
autonomous_db + session_scope (tasks._deploy ходит раздельными транзакциями).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_gc_dispatch(monkeypatch):  # noqa: ANN001, ANN202
    """Глушит Celery-постановку project.gc во всех endpoint-тестах файла (no-op).

    soft_delete_project вызывает _dispatch_project_gc → project_gc.apply_async (брокер).
    Для контракта soft-delete сам GC не нужен — мокаем; регистрируем вызовы для проверки,
    что DELETE владельца действительно ставит GC.
    """
    calls: list[str] = []
    import app.services.project_service as project_mod

    monkeypatch.setattr(project_mod, "_dispatch_project_gc", lambda pid: calls.append(pid))
    return calls


async def _make_project(session, user_id: str, *, prompt: str = "Landing") -> str:  # noqa: ANN001
    pid = new_project_id()
    session.add(Project(id=pid, user_id=user_id, prompt=prompt, title=None))
    await session.flush()
    return pid


# --- DELETE контракт (202 + soft-delete, владелец) ----------------------------


async def test_delete_returns_202_deleting_and_sets_deleted_at(
    client, auth_headers, session, seeded_user, _no_gc_dispatch
):
    pid = await _make_project(session, seeded_user.id)

    resp = await client.delete(f"/v1/projects/{pid}", headers=auth_headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body == {"project_id": pid, "status": "deleting"}

    # soft-delete-маркер выставлен.
    proj = await session.get(Project, pid)
    assert proj.deleted_at is not None
    # GC поставлен (async).
    assert _no_gc_dispatch == [pid]


async def test_deleted_project_disappears_from_list_and_detail_404(
    client, auth_headers, session, seeded_user, _no_gc_dispatch
):
    pid = await _make_project(session, seeded_user.id)
    other_pid = await _make_project(session, seeded_user.id, prompt="Survivor")

    # До DELETE — оба в листинге.
    lst = await client.get("/v1/projects", headers=auth_headers)
    ids_before = {p["id"] for p in lst.json()["projects"]}
    assert {pid, other_pid} <= ids_before

    await client.delete(f"/v1/projects/{pid}", headers=auth_headers)

    # GET /projects больше не содержит удаляемый; survivor остаётся.
    lst2 = await client.get("/v1/projects", headers=auth_headers)
    ids_after = {p["id"] for p in lst2.json()["projects"]}
    assert pid not in ids_after
    assert other_pid in ids_after

    # GET /projects/{pid} → 404 (не раскрываем, что проект существовал).
    detail = await client.get(f"/v1/projects/{pid}", headers=auth_headers)
    assert detail.status_code == 404


# --- cross-tenant / несуществующий → 404 --------------------------------------


async def test_delete_other_users_project_returns_404_no_softdelete(
    client, auth_headers, session, seeded_user, other_user, _no_gc_dispatch
):
    """Чужой pid → 404; deleted_at чужого проекта НЕ трогается (cross-tenant изоляция)."""
    foreign_pid = await _make_project(session, other_user.id, prompt="Foreign")

    resp = await client.delete(f"/v1/projects/{foreign_pid}", headers=auth_headers)
    assert resp.status_code == 404

    # Чужой проект не soft-delete'нут, GC не поставлен.
    foreign = await session.get(Project, foreign_pid)
    assert foreign.deleted_at is None
    assert _no_gc_dispatch == []


async def test_delete_nonexistent_project_returns_404(
    client, auth_headers, seeded_user, _no_gc_dispatch
):
    resp = await client.delete("/v1/projects/p_doesnotexist0000000000", headers=auth_headers)
    assert resp.status_code == 404
    assert _no_gc_dispatch == []


async def test_delete_requires_auth_401(client, session, seeded_user, _no_gc_dispatch):
    pid = await _make_project(session, seeded_user.id)
    resp = await client.delete(f"/v1/projects/{pid}")  # без Bearer
    assert resp.status_code == 401


# --- идемпотентность ----------------------------------------------------------


async def test_repeat_delete_softdeleted_returns_202_noop_keeps_deleted_at(
    client, auth_headers, session, seeded_user, _no_gc_dispatch
):
    """Повтор DELETE уже-soft-deleted проекта → 202 no-op; deleted_at НЕ перетирается."""
    pid = await _make_project(session, seeded_user.id)

    r1 = await client.delete(f"/v1/projects/{pid}", headers=auth_headers)
    assert r1.status_code == 202
    proj = await session.get(Project, pid)
    await session.refresh(proj)
    first_deleted_at = proj.deleted_at
    assert first_deleted_at is not None

    # Повторный DELETE — снова 202 (тот же терминальный путь), deleted_at тот же.
    r2 = await client.delete(f"/v1/projects/{pid}", headers=auth_headers)
    assert r2.status_code == 202
    await session.refresh(proj)
    assert proj.deleted_at == first_deleted_at, "повторный DELETE не должен перетирать deleted_at"
    # GC переставлен ещё раз (идемпотентен) — обе постановки зафиксированы.
    assert _no_gc_dispatch == [pid, pid]


async def test_delete_physically_removed_project_returns_404(
    client, auth_headers, session, seeded_user, _no_gc_dispatch
):
    """Строки уже физически нет (GC завершил hard-delete) → DELETE → 404."""
    pid = await _make_project(session, seeded_user.id)
    # Эмулируем завершённый GC: строка удалена.
    await session.execute(delete(Project).where(Project.id == pid))
    await session.flush()

    resp = await client.delete(f"/v1/projects/{pid}", headers=auth_headers)
    assert resp.status_code == 404


# --- soft-delete-фильтры: count_projects + /billing/me + quota-gate -----------


async def test_count_projects_excludes_soft_deleted(session, seeded_user):
    """entitlements.count_projects (projects_used) считает только deleted_at IS NULL."""
    from app.billing.entitlements import count_projects

    await _make_project(session, seeded_user.id, prompt="active-1")
    deleted_pid = await _make_project(session, seeded_user.id, prompt="to-delete")

    assert await count_projects(session, seeded_user.id) == 2

    # soft-delete одного → счётчик 1.
    proj = await session.get(Project, deleted_pid)
    proj.deleted_at = datetime.now(UTC)
    await session.flush()

    assert await count_projects(session, seeded_user.id) == 1


async def test_billing_me_projects_used_excludes_soft_deleted(
    client, auth_headers, session, seeded_user
):
    """GET /billing/me projects_used не считает soft-deleted проекты (ADR-011 §D)."""
    await _make_project(session, seeded_user.id, prompt="kept")
    deleted_pid = await _make_project(session, seeded_user.id, prompt="gone")
    proj = await session.get(Project, deleted_pid)
    proj.deleted_at = datetime.now(UTC)
    await session.flush()

    resp = await client.get("/v1/billing/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    # Один активный проект учтён, soft-deleted — нет.
    assert body["quota"]["projects_used"] == 1


async def test_quota_gate_max_projects_ignores_soft_deleted_slot(
    client, auth_headers, session, seeded_user, no_side_effects
):
    """free max_projects=1: soft-deleted проект НЕ занимает слот → новый POST /projects проходит.

    Перед DELETE активный проект = 1 (>= free max=1) → новый запрос ловил бы 402 (project_limit).
    После soft-delete projects_used=0 → новая генерация снова проходит gate (202).
    """
    # Активный проект+джоба исчерпывают free max_projects=1 и concurrency cap=1.
    pid = new_project_id()
    session.add(Project(id=pid, user_id=seeded_user.id, prompt="occupant", title=None))
    session.add(
        GenerationJob(
            id=new_job_id(),
            project_id=pid,
            user_id=seeded_user.id,
            state=JobState.FAILED,  # терминал: не занимает concurrency cap, но проект активен
            kind="generation",
            idempotency_key="occupant-key",
        )
    )
    await session.flush()

    # Контроль: пока проект активен, новый запрос упирается в project_limit (402).
    blocked = await client.post(
        "/v1/projects",
        json={"prompt": "blocked"},
        headers={**auth_headers, "Idempotency-Key": "blocked-key"},
    )
    assert blocked.status_code == 402
    assert blocked.json()["reason"] == "project_limit"

    # soft-delete активного проекта → слот освобождается.
    proj = await session.get(Project, pid)
    proj.deleted_at = datetime.now(UTC)
    await session.flush()

    # Теперь новый POST /projects проходит gate (projects_used=0 < max=1).
    ok = await client.post(
        "/v1/projects",
        json={"prompt": "after delete"},
        headers={**auth_headers, "Idempotency-Key": "after-key"},
    )
    assert ok.status_code == 202, f"после soft-delete слот свободен → 202: {ok.text}"


# --- race-guard: tasks._deploy на soft-deleted проекте ------------------------

_RG_UID = "u_racedeploy0000000000000"


async def _purge_rg() -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == _RG_UID)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == _RG_UID)
                    )
                )
                .scalars()
                .all()
            )
        )
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == _RG_UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == _RG_UID))
        await s.commit()


@pytest_asyncio.fixture
async def softdeleted_deploying_job(autonomous_db):  # noqa: ANN001, ANN201
    """Committed user + soft-deleted project + job в DEPLOYING (гонка GC↔in-flight виток)."""
    await _purge_rg()
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(
            User(
                id=_RG_UID,
                api_key_hash=hash_api_key("rg-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        # Проект уже soft-deleted (deleted_at выставлен): DELETE прошёл, GC ещё не снёс джобу.
        s.add(
            Project(
                id=pid,
                user_id=_RG_UID,
                prompt="Landing",
                title=None,
                deleted_at=datetime.now(UTC),
            )
        )
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=_RG_UID,
                state=JobState.DEPLOYING,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
            )
        )
        await s.commit()
    yield pid, jid
    await _purge_rg()


async def test_deploy_on_softdeleted_project_fails_project_deleted_no_deploy(
    softdeleted_deploying_job, monkeypatch
):
    """tasks._deploy, попавший на soft-deleted проект (гонка ADR-011 §C) → FAILED(project_deleted).

    Деплой НЕ происходит: ни docker run, ни health-check, ни строки site_deployments.
    Частичные ресурсы (если успели до soft-delete) снесёт project.gc.
    """
    pid, jid = softdeleted_deploying_job
    import app.pipeline.events as events
    import app.workers.tasks as tasks

    # Если код всё же дойдёт до деплоя — упадём явно (контракт: не должен).
    def _must_not_run(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("docker run не должен вызываться на soft-deleted проекте")

    async def _must_not_health(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("health-check не должен вызываться на soft-deleted проекте")

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(tasks.docker_deploy, "run_nginx_container", _must_not_run)
    monkeypatch.setattr(tasks.health, "wait_until_live", _must_not_health)
    monkeypatch.setattr(events, "publish_event", _noop_publish)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)

    await tasks._deploy(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "project_deleted"
        # Ни одной строки деплоя не создано.
        from app.db.models import SiteDeployment

        deps = (
            (await s.execute(select(SiteDeployment).where(SiteDeployment.project_id == pid)))
            .scalars()
            .all()
        )
        assert list(deps) == []
