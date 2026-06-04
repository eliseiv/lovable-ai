"""Integration: project.gc — полный GC ресурсов проекта (S4, ADR-011 §B; deploy §6).

Реальный Postgres (session_scope / autonomous_db). Внешние границы изолированы моками:
  - Docker teardown (docker_deploy.teardown_container) — spy, считает снос контейнеров;
  - S3 (storage.delete_prefix) — fake, считает batch-delete по префиксам;
  - host-volume (_remove_site_volume / shutil.rmtree) — spy на путь.

Покрывает 5 шагов GC (docs/modules/deploy/03-architecture.md §6, порядок обязателен):
  1. in-flight джобы → FAILED(project_deleted), снятие из active (concurrency-cap);
  2. teardown всех site-контейнеров проекта (любой status) docker rm -f site_{subdomain};
  3. удаление host-каталога {sites_host_root}/{pid};
  4. batch-delete S3 по per-job префиксам sources/dist/logs/specs (слэш → нет захвата
     соседних job_id с общим строковым началом);
  5. БД hard-delete в FK-порядке: site_deployments/revisions/job_events/answers/questions/
     llm_usage/generation_jobs/projects удалены; usage_counters/subscriptions/billing_events
     (агрегаты пользователя) НЕ тронуты.
Плюс идемпотентность повторного gc (no-op на отсутствующих ресурсах) и crash-resume.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.ids import (
    new_deployment_id,
    new_job_id,
    new_project_id,
    new_revision_id,
)
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import (
    Answer,
    BillingEvent,
    GenerationJob,
    JobEvent,
    LlmUsage,
    Project,
    Question,
    Revision,
    SiteDeployment,
    Subscription,
    UsageCounter,
    User,
)
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio

UID = "u_gcowner000000000000000"


class _FakeStorage:
    """In-memory S3: фиксирует delete_prefix-вызовы (prefix→batch_size) и «удаляет» объекты.

    Объекты сидируются по ключу. delete_prefix считает реально удалённые ключи под
    префиксом (точный per-job префикс со слэшем), что позволяет проверить отсутствие
    захвата соседних job_id.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.delete_prefix_calls: list[tuple[str, int]] = []

    async def delete_prefix(self, prefix: str, *, batch_size: int) -> int:
        self.delete_prefix_calls.append((prefix, batch_size))
        matched = [k for k in self.objects if k.startswith(prefix)]
        for k in matched:
            del self.objects[k]
        return len(matched)


async def _purge(uid: str) -> None:
    """FK-safe очистка всех данных пользователя (вне rollback-сессии)."""
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == uid)
                    )
                )
                .scalars()
                .all()
            )
        )
        # Снять projects.current_revision_id (use_alter FK) до удаления revisions.
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        # Также проекты, у которых нет джоб (на всякий случай по user_id).
        orphan_pids = (
            (await s.execute(select(Project.id).where(Project.user_id == uid))).scalars().all()
        )
        for pid in orphan_pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        all_pids = set(pids) | set(orphan_pids)
        if all_pids:
            await s.execute(delete(SiteDeployment).where(SiteDeployment.project_id.in_(all_pids)))
            await s.execute(delete(Revision).where(Revision.project_id.in_(all_pids)))
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            await s.execute(delete(Answer).where(Answer.job_id.in_(job_ids)))
            await s.execute(delete(Question).where(Question.job_id.in_(job_ids)))
            await s.execute(delete(LlmUsage).where(LlmUsage.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in all_pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(Subscription).where(Subscription.user_id == uid))
        await s.execute(delete(UsageCounter).where(UsageCounter.user_id == uid))
        await s.execute(delete(BillingEvent).where(BillingEvent.user_id == uid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


@pytest_asyncio.fixture
async def gc_project(autonomous_db):  # noqa: ANN001, ANN201
    """Committed богатый проект: 2 джобы (1 in-flight DEPLOYING, 1 LIVE), 2 деплоя,
    1 ревизия, job_events/answers/questions/llm_usage, + user-агрегаты (subscription,
    usage_counter, billing_event) которые GC НЕ должен трогать.

    Возвращает dict с id для ассертов. Сосед-проект соседнего job_id с общим префиксом
    добавляется в S3-тесте отдельно (изоляция).
    """
    await _purge(UID)
    pid = new_project_id()
    job_live = new_job_id()
    job_inflight = new_job_id()
    rev_id = new_revision_id()
    dep_active = new_deployment_id()
    dep_failed = new_deployment_id()

    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("gc-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="Landing", title=None))
        s.add(
            GenerationJob(
                id=job_live,
                project_id=pid,
                user_id=UID,
                state=JobState.LIVE,
                kind="generation",
                budget_usd=Decimal("5.0000"),
            )
        )
        s.add(
            GenerationJob(
                id=job_inflight,
                project_id=pid,
                user_id=UID,
                state=JobState.DEPLOYING,  # in-flight: должен стать FAILED(project_deleted)
                kind="generation",
                budget_usd=Decimal("5.0000"),
            )
        )
        await s.flush()
        s.add(
            Revision(
                id=rev_id,
                project_id=pid,
                revision_no=1,
                source_artifact_ref=f"sources/{job_live}/source.tgz",
                created_from_job_id=job_live,
                is_good=True,
            )
        )
        await s.flush()
        proj = await s.get(Project, pid)
        proj.current_revision_id = rev_id
        # Два деплоя: active + failed (GC сносит контейнеры по ВСЕМ статусам).
        s.add(
            SiteDeployment(
                id=dep_active,
                project_id=pid,
                revision_id=rev_id,
                subdomain="aaaaaaaaaaaaaaaa",
                live_url="http://aaaaaaaaaaaaaaaa.apps.localhost/",
                dist_artifact_ref=f"dist/{job_live}/dist.tgz",
                container_id="cid_active",
                status="active",
            )
        )
        s.add(
            SiteDeployment(
                id=dep_failed,
                project_id=pid,
                revision_id=rev_id,
                subdomain="bbbbbbbbbbbbbbbb",
                live_url="http://bbbbbbbbbbbbbbbb.apps.localhost/",
                dist_artifact_ref=f"dist/{job_inflight}/dist.tgz",
                container_id=None,
                status="failed",
            )
        )
        # Дочерние строки джоб.
        s.add(JobEvent(job_id=job_live, event_type="state_changed", to_state="LIVE", payload={}))
        q_id = "q_gc00000000000000000000"
        s.add(Question(id=q_id, job_id=job_live, position=1, text="?"))
        s.add(Answer(id="a_gc00000000000000000000", question_id=q_id, job_id=job_live, text="yes"))
        s.add(
            LlmUsage(
                job_id=job_live,
                agent="agent1",
                model="sonnet",
                input_tokens=10,
                output_tokens=5,
                cost_usd=Decimal("0.0100"),
            )
        )
        # User-агрегаты (НЕ трогаются GC).
        s.add(
            Subscription(
                id="s_gc00000000000000000000",
                user_id=UID,
                access_level="pro",
                status="active",
                will_renew=True,
                raw={},
                synced_at=datetime.now(UTC),
            )
        )
        s.add(UsageCounter(user_id=UID, period="2026-06", generations_used=2))
        s.add(
            BillingEvent(
                adapty_event_id="evt_gc_0001",
                event_type="subscription_renewed",
                user_id=UID,
                payload={},
            )
        )
        # Проект soft-deleted (DELETE уже прошёл) — GC снимает hard-delete.
        proj.deleted_at = datetime.now(UTC)
        await s.commit()

    yield {
        "pid": pid,
        "job_live": job_live,
        "job_inflight": job_inflight,
        "rev_id": rev_id,
        "subdomains": ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"],
    }
    await _purge(UID)


def _wire_gc(monkeypatch, storage):  # noqa: ANN001, ANN202
    """Мокает внешние границы project.gc: storage, docker teardown, host-volume.

    Возвращает (teardown_calls, removed_paths) для ассертов на шаги 2/3.
    """
    import app.deploy.project_gc as gc

    teardown_calls: list[str] = []
    removed_paths: list[str] = []

    monkeypatch.setattr(gc, "get_storage", lambda: storage)
    monkeypatch.setattr(
        gc.docker_deploy, "teardown_container", lambda name: teardown_calls.append(name)
    )

    real_settings = gc.get_settings()

    def _spy_rmtree(path, ignore_errors=False):  # noqa: ANN001, ANN202, FBT002
        removed_paths.append(str(path))

    monkeypatch.setattr(gc.shutil, "rmtree", _spy_rmtree)
    return teardown_calls, removed_paths, real_settings


# --- Полный GC: все 5 шагов ---------------------------------------------------


async def test_gc_full_sweep_all_steps(gc_project, monkeypatch):
    from app.deploy.project_gc import _run_gc

    ctx = gc_project
    pid, job_live, job_inflight = ctx["pid"], ctx["job_live"], ctx["job_inflight"]

    storage = _FakeStorage()
    # Сидируем S3-артефакты обоих job_id (sources/dist/logs/specs).
    for jid in (job_live, job_inflight):
        for prefix in (f"sources/{jid}/", f"dist/{jid}/", f"logs/{jid}/", f"specs/{jid}/"):
            storage.objects[prefix + "obj"] = b"x"
    teardown_calls, removed_paths, settings = _wire_gc(monkeypatch, storage)

    await _run_gc(pid)

    # Шаг 2: оба контейнера снесены (active + failed, по subdomain).
    assert sorted(teardown_calls) == ["site_aaaaaaaaaaaaaaaa", "site_bbbbbbbbbbbbbbbb"]

    # Шаг 3: host-каталог проекта удалён ровно по {sites_host_root}/{pid}.
    expected_dir = str(__import__("pathlib").Path(settings.sites_host_root) / pid)
    assert expected_dir in removed_paths

    # Шаг 4: все S3-артефакты обоих job_id удалены (8 префиксов).
    assert storage.objects == {}, "все per-job префиксы должны быть удалены"
    deleted_prefixes = {c[0] for c in storage.delete_prefix_calls}
    for jid in (job_live, job_inflight):
        assert {
            f"sources/{jid}/",
            f"dist/{jid}/",
            f"logs/{jid}/",
            f"specs/{jid}/",
        } <= deleted_prefixes

    # Шаг 5: БД hard-delete всех строк проекта; user-агрегаты целы.
    async with session_scope() as s:
        assert await s.get(Project, pid) is None
        assert await s.get(GenerationJob, job_live) is None
        assert await s.get(GenerationJob, job_inflight) is None
        assert (
            await s.scalar(
                select(func.count())
                .select_from(SiteDeployment)
                .where(SiteDeployment.project_id == pid)
            )
            == 0
        )
        assert (
            await s.scalar(
                select(func.count()).select_from(Revision).where(Revision.project_id == pid)
            )
            == 0
        )
        for model in (JobEvent, Answer, Question, LlmUsage):
            cnt = await s.scalar(
                select(func.count())
                .select_from(model)
                .where(model.job_id.in_([job_live, job_inflight]))
            )
            assert cnt == 0, model.__name__

        # User-агрегаты НЕ тронуты.
        assert (
            await s.scalar(
                select(func.count()).select_from(Subscription).where(Subscription.user_id == UID)
            )
            == 1
        )
        assert (
            await s.scalar(
                select(func.count()).select_from(UsageCounter).where(UsageCounter.user_id == UID)
            )
            == 1
        )
        assert (
            await s.scalar(
                select(func.count()).select_from(BillingEvent).where(BillingEvent.user_id == UID)
            )
            == 1
        )
        # User-строка тоже цела (агрегаты ссылаются на неё).
        assert await s.get(User, UID) is not None


# --- Шаг 1 отдельно: in-flight джоба → FAILED(project_deleted) -----------------


async def test_gc_cancels_inflight_job_sets_project_deleted(gc_project, monkeypatch):
    """Шаг 1: все НЕ-терминальные джобы проекта → FAILED(project_deleted) (ADR-011 §C).

    Нормативно (ADR-011 §C): отменяются все джобы с state ∉ TERMINAL_STATES (= {FAILED}),
    т.е. любой активный/устойчивый не-FAILED state, включая LIVE — снимает из active_jobs
    (concurrency-cap) и диспетчеризации. В фикстуре 2 не-FAILED джобы (DEPLOYING + LIVE) →
    обе отменяются. Уже-FAILED джобы (если были) не трогаются. reason-код = project_deleted.
    """
    from app.deploy.project_gc import _cancel_inflight_jobs

    ctx = gc_project
    pid, job_live, job_inflight = ctx["pid"], ctx["job_live"], ctx["job_inflight"]

    async with session_scope() as s:
        cancelled = await _cancel_inflight_jobs(s, pid)
        await s.commit()
    # DEPLOYING + LIVE — обе не-терминальны (TERMINAL_STATES = {FAILED}) → отменены.
    assert cancelled == 2, "все не-FAILED джобы проекта отменяются (ADR-011 §C)"

    async with session_scope() as s:
        for jid in (job_inflight, job_live):
            job = await s.get(GenerationJob, jid)
            assert job.state == JobState.FAILED
            assert job.failure_reason == "project_deleted", jid


# --- Шаг 4 изоляция: per-job префикс со слэшем не захватывает соседний job_id --


async def test_gc_s3_prefix_does_not_capture_sibling_job(gc_project, monkeypatch):
    """Per-job префикс sources/{job_id}/ (слэш в конце) НЕ удаляет объекты соседнего
    job_id с общим строковым началом (sources/{job_id}_evil/...).

    Регресс на захват: без завершающего слэша sources/j_abc удалил бы и sources/j_abcdef.
    """
    from app.deploy.project_gc import _run_gc

    ctx = gc_project
    pid, job_live, job_inflight = ctx["pid"], ctx["job_live"], ctx["job_inflight"]

    storage = _FakeStorage()
    # Артефакты проекта.
    for jid in (job_live, job_inflight):
        storage.objects[f"sources/{jid}/source.tgz"] = b"mine"
    # Сосед: тот же строковый префикс + суффикс (НЕ должен быть удалён).
    sibling_key = f"sources/{job_live}_sibling/source.tgz"
    storage.objects[sibling_key] = b"not-mine"

    _wire_gc(monkeypatch, storage)
    await _run_gc(pid)

    assert sibling_key in storage.objects, "соседний job_id с общим префиксом не должен удаляться"
    # Свои артефакты — удалены.
    assert f"sources/{job_live}/source.tgz" not in storage.objects


# --- B/#10: project.gc дочищает per-attempt логи build.{n}/deploy.{n}/agent.{n} ---


async def test_gc_sweeps_per_attempt_logs_under_logs_prefix(gc_project, monkeypatch):
    """ADR-022 §Ретеншн: все per-attempt логи (build.{n}/deploy.{n}/agent.{n}) под
    logs/{job_id}/ подчищаются тем же batch-delete по префиксу logs/{job_id}/ в project.gc —
    отдельной очистки не требуется (prefix-захват по logs/{job_id}/)."""
    from app.deploy.project_gc import _run_gc

    ctx = gc_project
    pid, job_live, job_inflight = ctx["pid"], ctx["job_live"], ctx["job_inflight"]

    storage = _FakeStorage()
    # Сидируем per-attempt логи нескольких витков для обоих job_id под logs/{job_id}/.
    for jid in (job_live, job_inflight):
        for n in (0, 1, 2):
            storage.objects[f"logs/{jid}/build.{n}.log"] = b"build log"
            storage.objects[f"logs/{jid}/deploy.{n}.log"] = b"deploy log"
            storage.objects[f"logs/{jid}/agent.{n}.log"] = b"agent log"
    seeded = dict(storage.objects)
    assert seeded, "должны быть засеяны per-attempt логи"

    _wire_gc(monkeypatch, storage)
    await _run_gc(pid)

    # Все per-attempt логи обоих job_id снесены (под захваченным префиксом logs/{job_id}/).
    for key in seeded:
        assert key not in storage.objects, f"per-attempt лог {key} должен быть удалён project.gc"
    assert storage.objects == {}
    # Префикс logs/{job_id}/ участвовал в batch-delete для каждого job_id.
    deleted_prefixes = {c[0] for c in storage.delete_prefix_calls}
    for jid in (job_live, job_inflight):
        assert f"logs/{jid}/" in deleted_prefixes


# --- Идемпотентность / crash-resume -------------------------------------------


async def test_gc_idempotent_second_run_is_noop(gc_project, monkeypatch):
    """Повторный project.gc на уже-снесённом проекте → no-op (строки нет, ресурсы пусты)."""
    from app.deploy.project_gc import _run_gc

    ctx = gc_project
    pid = ctx["pid"]

    storage = _FakeStorage()
    teardown_calls, removed_paths, _ = _wire_gc(monkeypatch, storage)

    await _run_gc(pid)  # первый прогон сносит всё
    teardown_calls.clear()
    removed_paths.clear()
    storage.delete_prefix_calls.clear()

    # Второй прогон: проект уже физически удалён → ранний выход (project is None).
    await _run_gc(pid)
    assert teardown_calls == [], "повторный GC не дёргает teardown (строки нет → early-return)"

    async with session_scope() as s:
        assert await s.get(Project, pid) is None


async def test_gc_resumable_after_partial_db_intact_when_resources_gone(gc_project, monkeypatch):
    """Crash-resume: если внешние ресурсы уже снесены (teardown/S3 idempotent no-op),
    повторный GC всё равно доводит БД-каскад до конца (hard-delete строк).

    Моделируем «краш после шагов 2-4, до шага 5»: ресурсов нет (storage пуст,
    docker rm -f идемпотентен), но БД-строки ещё на месте. Повторный _run_gc обязан
    снести БД-каскад без ошибок.
    """
    from app.deploy.project_gc import _run_gc

    ctx = gc_project
    pid = ctx["pid"]

    # storage пуст (S3 уже вычищен прошлым витком) → delete_prefix вернёт 0 (no-op).
    storage = _FakeStorage()
    teardown_calls, _, _ = _wire_gc(monkeypatch, storage)

    await _run_gc(pid)

    # teardown идемпотентен (контейнеров может не быть — мок просто фиксирует вызов),
    # главное — БД доведена до конца.
    async with session_scope() as s:
        assert await s.get(Project, pid) is None
        assert (
            await s.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.project_id == pid)
            )
            == 0
        )
