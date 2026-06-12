"""Integration: видимый статус EDITING для edit-джобы (ADR-030, docs/06 §S5 Edits).

Реальный Postgres (session_scope autonomous_db). Внешние границы (Agent 4 editor,
S3-storage, dispatch_for_state, redeploy) — моки. Покрывает task-уровень _edit:
  - (2) edit стартует в EDITING ДО Agent 4 editor (CREATED → EDITING, событие, heartbeat);
  - (4) crash-resume: повторный _edit при EDITING переобрабатывает editor, НЕ дублирует
        переход/событие state_changed, count_edit_start идемпотентен;
  - (5) guard: state не в {CREATED, EDITING} или kind != edit → skip (no-op);
  - (6) CAS-терминальность: FAILED-джоба → transition(EDITING) no-op (ADR-029).

dispatcher EDITING → task_edit покрыт в tests/unit/test_dispatcher.py;
reconciler-скоуп EDITING (fail-stuck) — в tests/integration/test_adr019_terminalization.py;
миграция enum + happy edit-цикл E2E — test_migration_editing_enum.py / e2e edit-flow.
"""

from __future__ import annotations

import io
import json
import tarfile
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.ids import new_job_id, new_project_id, new_revision_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import EditUsageCounter, GenerationJob, JobEvent, Project, Revision, User
from app.db.session import session_scope
from app.pipeline.agents.agent4 import Agent4Result
from app.schemas.agent_output import ValidatedFile, ValidatedTree
from app.workers import tasks as worker_tasks

pytestmark = pytest.mark.asyncio

UID = "u_editing0000000001a"


# --- Fakes ---


class _FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key, data, content_type="application/octet-stream"):  # noqa: ANN001, ANN202
        self.objects[key] = data
        return key

    async def put_text(self, key, text, content_type="text/plain"):  # noqa: ANN001, ANN202
        self.objects[key] = text.encode("utf-8")
        return key

    async def get_bytes(self, key):  # noqa: ANN001, ANN202
        return self.objects[key]


def _src_tgz() -> bytes:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {"index.html": b"<html></html>", "package.json": pkg.encode()}.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _editor_tree() -> ValidatedTree:
    return ValidatedTree(
        files=(
            ValidatedFile(path="index.html", encoding="utf8", content_bytes=b"<html>edited</html>"),
            ValidatedFile(
                path="package.json",
                encoding="utf8",
                content_bytes=json.dumps(
                    {
                        "name": "s",
                        "scripts": {"build": "vite build"},
                        "devDependencies": {"vite": "^5"},
                    }
                ).encode(),
            ),
        ),
        entry="index.html",
        build_command="vite build",
        build_output_dir="dist",
    )


async def _purge() -> None:
    async with session_scope() as s:
        pids = (await s.execute(select(Project.id).where(Project.user_id == UID))).scalars().all()
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        for pid in pids:
            await s.execute(delete(Revision).where(Revision.project_id == pid))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        await s.execute(delete(Project).where(Project.user_id == UID))
        await s.execute(delete(EditUsageCounter).where(EditUsageCounter.user_id == UID))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


async def _seed_edit_job(state: JobState, *, kind: str = "edit") -> dict:
    """Проект LIVE на good-ревизии rev1 + edit-джоба в заданном state.

    edit_requested указывает base_revision_id=rev1 (источник правки для _load_edit_request).
    """
    pid = new_project_id()
    src_jid = new_job_id()
    edit_jid = new_job_id()
    rid1 = new_revision_id()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("editing-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="p", title=None))
        s.add(
            GenerationJob(
                id=src_jid,
                project_id=pid,
                user_id=UID,
                state=JobState.LIVE,
                kind="generation",
                spec_tz="spec text",
            )
        )
        s.add(
            GenerationJob(
                id=edit_jid,
                project_id=pid,
                user_id=UID,
                state=state,
                kind=kind,
                spec_tz="spec text",
                budget_usd=Decimal("5.0000"),
            )
        )
        await s.flush()
        s.add(
            Revision(
                id=rid1,
                project_id=pid,
                revision_no=1,
                source_artifact_ref="s3://src/1",
                created_from_job_id=src_jid,
                is_good=True,
            )
        )
        await s.flush()
        proj = await s.get(Project, pid)
        proj.current_revision_id = rid1
        from app.pipeline.events import record_event

        await record_event(
            s,
            edit_jid,
            "edit_requested",
            payload={"instruction": "make it blue", "base_revision_id": rid1},
        )
        await s.commit()
    return {"pid": pid, "edit_jid": edit_jid, "rid1": rid1, "src_ref": "s3://src/1"}


@pytest_asyncio.fixture
async def edit_env(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    await _purge()
    storage = _FakeStorage()
    # base-revision source.tgz доступен по своему ключу для editor-входа.
    storage.objects["s3://src/1"] = _src_tgz()
    monkeypatch.setattr(worker_tasks, "get_storage", lambda: storage)
    import app.storage.s3 as s3mod

    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)

    # dispatch_for_state → no-op (без Celery): EDITING → BUILDING переход внутри _edit мы
    # проверяем по состоянию, а постановку следующей таски не дёргаем.
    dispatched: list = []
    monkeypatch.setattr(
        worker_tasks, "dispatch_for_state", lambda *a, **k: dispatched.append((a, k))
    )

    # Agent 4 editor → валидное новое дерево (счётчик вызовов для crash-resume идемпотентности).
    editor_calls = {"n": 0}

    async def _fake_editor(settings, **kwargs):  # noqa: ANN001, ANN202
        editor_calls["n"] += 1
        # Прогоняем хук before_call (budget/wall-clock guard) и after_call (usage), как реальный.
        await kwargs["before_call"]()
        return Agent4Result(call=None, tree=_editor_tree(), unrecoverable=None)

    monkeypatch.setattr(worker_tasks, "run_agent4_editor", _fake_editor)
    yield {"storage": storage, "dispatched": dispatched, "editor_calls": editor_calls}
    await _purge()


async def _events(job_id: str, event_type: str | None = None) -> list[JobEvent]:
    async with session_scope() as s:
        q = select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
        if event_type is not None:
            q = q.where(JobEvent.event_type == event_type)
        return list((await s.execute(q)).scalars().all())


# --- (2) edit стартует в EDITING ДО Agent 4 editor ---


async def test_edit_transitions_to_editing_before_editor(edit_env):
    """Первый вход _edit (CREATED, kind=edit): CREATED → EDITING ДО Agent 4 editor.

    Проверяем: editor вызван (правка применена → BUILDING), но переход в EDITING случился —
    видимое событие state_changed(to=EDITING) записано ДО source_packed/BUILDING (по порядку id).
    """
    data = await _seed_edit_job(JobState.CREATED)

    # Захватим last_transition_at ДО запуска.
    async with session_scope() as s:
        before = await s.get(GenerationJob, data["edit_jid"])
        lt_before = before.last_transition_at

    await worker_tasks._edit(data["edit_jid"])

    assert edit_env["editor_calls"]["n"] == 1

    # state_changed(to=EDITING) присутствует и предшествует BUILDING-переходу.
    state_changes = await _events(data["edit_jid"], "state_changed")
    to_states = [e.to_state for e in state_changes]
    assert "EDITING" in to_states, to_states
    assert "BUILDING" in to_states, to_states
    assert to_states.index("EDITING") < to_states.index("BUILDING")

    # Переход EDITING несёт from_state=CREATED (первый видимый переход edit-flow).
    editing_evt = next(e for e in state_changes if e.to_state == "EDITING")
    assert editing_evt.from_state == "CREATED"

    # last_transition_at сдвинут (heartbeat → нет ложного fail-stuck в CREATED).
    async with session_scope() as s:
        after = await s.get(GenerationJob, data["edit_jid"])
        assert after.last_transition_at > lt_before
        # Happy edit: после успешного editor джоба ушла в BUILDING.
        assert after.state == JobState.BUILDING


async def test_edit_visible_status_not_created_when_editor_runs(edit_env, monkeypatch):
    """Видимый статус во время editor — EDITING, не CREATED (корень UX-бага ADR-030).

    Editor «застывает» (проверяем job.state внутри editor-вызова): к моменту работы Agent 4
    джоба уже в EDITING, а не CREATED.
    """
    data = await _seed_edit_job(JobState.CREATED)
    observed: dict = {}

    async def _editor_observe_state(settings, **kwargs):  # noqa: ANN001, ANN202
        await kwargs["before_call"]()
        async with session_scope() as s:
            j = await s.get(GenerationJob, data["edit_jid"])
            observed["state"] = j.state
        return Agent4Result(call=None, tree=_editor_tree(), unrecoverable=None)

    monkeypatch.setattr(worker_tasks, "run_agent4_editor", _editor_observe_state)
    await worker_tasks._edit(data["edit_jid"])
    assert observed["state"] == JobState.EDITING


# --- (4) crash-resume: повторный _edit при EDITING ---


async def test_edit_resume_from_editing_reprocesses_editor_without_dup_transition(edit_env):
    """Resume из EDITING: editor переобрабатывается, переход EDITING→EDITING НЕ дублирует
    событие state_changed/heartbeat, count_edit_start идемпотентен (ADR-030 §C).
    """
    data = await _seed_edit_job(JobState.EDITING)

    # Повторный вход на state=EDITING (имитация crash-resume по dispatch_for_state(EDITING)).
    await worker_tasks._edit(data["edit_jid"])

    # Editor переобработан (guard принял EDITING, не skip).
    assert edit_env["editor_calls"]["n"] == 1

    # НЕТ state_changed(to=EDITING): resume пропускает переход (no-op), чтобы не дублировать
    # heartbeat/событие (job.state уже EDITING → ветка transition пропущена в _edit).
    editing_changes = [
        e for e in await _events(data["edit_jid"], "state_changed") if e.to_state == "EDITING"
    ]
    assert editing_changes == [], "resume не должен писать повторный state_changed(EDITING)"

    # Успешный editor увёл в BUILDING.
    async with session_scope() as s:
        after = await s.get(GenerationJob, data["edit_jid"])
        assert after.state == JobState.BUILDING

    # count_edit_start: ровно один edit_usage_counted-маркер за джобу.
    counted = await _events(data["edit_jid"], "edit_usage_counted")
    assert len(counted) == 1


async def test_edit_resume_twice_count_edit_start_idempotent(edit_env, monkeypatch):
    """Двойной resume на EDITING: count_edit_start идемпотентен по job_id (один маркер, used=1).

    Editor мокается БЕЗ перехода в BUILDING (unrecoverable=None но дерево есть → дошёл бы до
    BUILDING; чтобы остаться в EDITING для второго прохода, мокаем editor, бросающий путь
    после инкремента). Проще: первый проход уводит в BUILDING; восстановим EDITING и
    прогоним ещё раз — count_edit_start не должен начислить второй раз.
    """
    from app.billing import usage

    data = await _seed_edit_job(JobState.EDITING)

    # Первый resume-проход (учитывает edit_usage один раз, уходит в BUILDING).
    await worker_tasks._edit(data["edit_jid"])
    async with session_scope() as s:
        used_after_1 = await usage.get_edit_usage(s, UID)
    assert used_after_1 == 1

    # Возвращаем джобу в EDITING (имитация ещё одного crash-resume того же job_id).
    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        job.state = JobState.EDITING
        await s.commit()

    await worker_tasks._edit(data["edit_jid"])
    async with session_scope() as s:
        used_after_2 = await usage.get_edit_usage(s, UID)
    # Идемпотентность по job_id: повторный старт того же job_id НЕ инкрементирует.
    assert used_after_2 == 1
    counted = await _events(data["edit_jid"], "edit_usage_counted")
    assert len(counted) == 1


async def test_edit_resume_does_not_drop_heartbeat_below_first_pass(edit_env):
    """Resume на EDITING не сбрасывает heartbeat назад: last_transition_at не уменьшается."""
    data = await _seed_edit_job(JobState.EDITING)
    async with session_scope() as s:
        before = await s.get(GenerationJob, data["edit_jid"])
        lt_before = before.last_transition_at
    await worker_tasks._edit(data["edit_jid"])
    async with session_scope() as s:
        after = await s.get(GenerationJob, data["edit_jid"])
    # BUILDING-переход двигает last_transition_at вперёд (или равен, если совпали часы).
    assert after.last_transition_at >= lt_before


# --- (5) guard skip ---


async def test_edit_skips_when_state_not_created_or_editing(edit_env):
    """state не в {CREATED, EDITING} (например LIVE) → _edit no-op (editor не вызван)."""
    data = await _seed_edit_job(JobState.LIVE)
    await worker_tasks._edit(data["edit_jid"])
    assert edit_env["editor_calls"]["n"] == 0
    async with session_scope() as s:
        after = await s.get(GenerationJob, data["edit_jid"])
        assert after.state == JobState.LIVE  # не тронут


async def test_edit_skips_when_kind_not_edit(edit_env):
    """kind != 'edit' (generation в CREATED) → _edit no-op (это путь task_interview)."""
    data = await _seed_edit_job(JobState.CREATED, kind="generation")
    await worker_tasks._edit(data["edit_jid"])
    assert edit_env["editor_calls"]["n"] == 0
    async with session_scope() as s:
        after = await s.get(GenerationJob, data["edit_jid"])
        assert after.state == JobState.CREATED  # не тронут


# --- (6) CAS-терминальность: FAILED → transition(EDITING) no-op (ADR-029) ---


async def test_transition_to_editing_noop_on_terminal_failed(edit_env):
    """Джоба уже FAILED → transition(EDITING) — no-op: state остаётся FAILED, EDITING не пишется.

    CAS-барьер ADR-029: EDITING (нетерминал) не может перезатереть терминал FAILED.
    """
    from app.pipeline.events import transition

    data = await _seed_edit_job(JobState.FAILED)
    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        applied = await transition(s, job, JobState.EDITING, event_type="state_changed")
        await s.commit()
    assert applied is False
    async with session_scope() as s:
        after = await s.get(GenerationJob, data["edit_jid"])
        assert after.state == JobState.FAILED
    # Нет события state_changed(to=EDITING) поверх FAILED.
    editing_changes = [
        e for e in await _events(data["edit_jid"], "state_changed") if e.to_state == "EDITING"
    ]
    assert editing_changes == []
