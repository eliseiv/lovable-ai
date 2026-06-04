"""Integration: восстановительный цикл FIXING — task_fix + гарды + no-progress.

Реальный Postgres (session_scope/autonomous_db). Внешние границы — S3 (FakeStorage),
Agent 4 (фейк-ClaudeAgentClient), dispatch_for_state (спай) — мокаются.

Покрывает (docs §A-D, ADR-005/006):
- task_fix валидный патч → новая ревизия текущей джобы + retry_count++ → BUILDING;
- task_fix невалидный патч → fix-неудача: llm_usage записан, остаёмся в FIXING,
  retry_count НЕ инкрементируется, re-dispatch FIXING;
- unrecoverable → FAILED(fixer_gave_up);
- гарды (a)/(b)/(c) на входе в FIXING → FAILED(reason);
- no-progress (d): реальный повтор сигнатуры на НОВОМ событии → FAILED(no_progress);
- crash-resume: reconciler ре-диспетчеризовал task_fix по тому же логу (флаг сброшен) →
  Agent 4 вызывается, НЕ no_progress;
- invalid-patch loop: agent_output_invalid дважды (новое событие) → no_progress;
- вход Agent 4 = дерево последней ревизии ТЕКУЩЕЙ джобы (created_from_job_id=job_id,
  max revision_no), не глобальный max по проекту.
"""

from __future__ import annotations

import io
import json
import tarfile
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id, new_revision_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import (
    Answer,
    GenerationJob,
    JobEvent,
    LlmUsage,
    Project,
    Question,
    Revision,
    SiteDeployment,
    User,
)
from app.db.session import session_scope
from app.pipeline.agents.claude_client import AgentCall
from app.pipeline.failure_signature import build_failure_log
from app.storage import s3

pytestmark = pytest.mark.asyncio

UID = "u_fixloopowner000000000"


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


def _src_tgz(extra: str = "") -> bytes:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    files = {"index.html": b"<html></html>", "package.json": pkg.encode()}
    if extra:
        files["src/main.ts"] = extra.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _valid_patch_json() -> str:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    return json.dumps(
        {
            "files": [
                {"path": "index.html", "encoding": "utf8", "content": "<html>fixed</html>"},
                {"path": "package.json", "encoding": "utf8", "content": pkg},
            ],
            "entry": "index.html",
            "build": {"tool": "vite", "command": "npm ci && vite build", "output_dir": "dist"},
        }
    )


def _call(text: str) -> AgentCall:
    return AgentCall(
        text=text,
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.0010"),
    )


class _FakeClient:
    """Фейк ClaudeAgentClient: форсированный tool-use (ADR-020 §I.1).

    Возвращает заранее заданный JSON-текст (по очереди) как tool_input — модель
    «заполняет аргументы инструмента» (structured-output читается из tool_use.input,
    не из текста). Невалидный JSON в очереди → tool_input=None (граничный случай /
    отказ tool-use), тогда structured-слой применяет толерантный парсинг к text (§I.2).
    """

    _texts: list[str] = []
    captured: list[str] = []

    def __init__(self, settings) -> None:  # noqa: ANN001
        pass

    async def run_agent_tool(  # noqa: ANN201
        self,
        *,
        model,
        system_prompt,
        user_content,
        tool_name,
        input_schema,  # noqa: ANN001, ANN003
    ):
        from app.pipeline.agents.claude_client import AgentToolCall

        type(self).captured.append(user_content)
        text = type(self)._texts.pop(0) if type(self)._texts else "{}"
        try:
            tool_input = json.loads(text)
            if not isinstance(tool_input, dict):
                tool_input = None
        except ValueError:
            tool_input = None
        return AgentToolCall(tool_input=tool_input, text=text, call=_call(text))


async def _purge(uid: str) -> None:
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
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        if pids:
            await s.execute(delete(SiteDeployment).where(SiteDeployment.project_id.in_(pids)))
        if job_ids:
            await s.execute(delete(LlmUsage).where(LlmUsage.job_id.in_(job_ids)))
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            await s.execute(delete(Answer).where(Answer.job_id.in_(job_ids)))
            await s.execute(delete(Question).where(Question.job_id.in_(job_ids)))
            await s.execute(delete(Revision).where(Revision.created_from_job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


@pytest_asyncio.fixture
async def fixing_job(autonomous_db):  # noqa: ANN001, ANN201
    """Committed user+project+job в FIXING с записанным failure_log_ref + спекой.

    failure_event_pending=True (как после enter_fixing). Возвращает (pid, jid, storage,
    failure_class, build_error_log) для точечной настройки в тестах.
    """
    pid = new_project_id()
    jid = new_job_id()
    await _purge(UID)
    storage = _FakeStorage()

    build_log = build_failure_log(
        failure_class="build_error",
        body="src/main.ts:1:1 - error TS2304: Cannot find module './x'\nexit code 2\n",
        revision_no=1,
        exit_code=2,
    )
    log_ref = s3.build_log_key(jid)
    storage.objects[log_ref] = build_log.encode("utf-8")
    # source.tgz по детерминированному ключу джобы (нет ревизии-кандидата ещё).
    storage.objects[s3.source_key(jid)] = _src_tgz()

    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("fl-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="Landing", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=JobState.FIXING,
                kind="generation",
                spec_tz="# Spec",
                failure_log_ref=log_ref,
                failure_event_pending=True,
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
                max_fix_attempts=3,
                retry_count=0,
            )
        )
        await s.commit()
    yield pid, jid, storage
    await _purge(UID)


def _wire(monkeypatch, storage):  # noqa: ANN001, ANN202
    """Мокает storage + dispatch (спай) + Agent4 фейк-клиент. Возвращает (tasks, dispatched)."""
    import app.pipeline.agents.agent4 as agent4_mod
    import app.storage.s3 as s3mod
    import app.workers.tasks as tasks

    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(s3mod, "get_storage", lambda: storage)

    dispatched: list = []
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda jid, st: dispatched.append((jid, st)))

    import app.pipeline.events as events

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop_publish)

    _FakeClient.captured = []
    monkeypatch.setattr(agent4_mod, "ClaudeAgentClient", _FakeClient)
    return tasks, dispatched


async def _llm_usage_count(jid: str) -> int:
    async with session_scope() as s:
        return (
            await s.execute(
                select(func.count()).select_from(LlmUsage).where(LlmUsage.job_id == jid)
            )
        ).scalar_one()


# --- task_fix: валидный патч → BUILDING + retry_count++ + новая ревизия ---


async def test_fix_valid_patch_transitions_to_building(fixing_job, monkeypatch):
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.BUILDING
        assert job.retry_count == 1  # инкремент ровно один (применённый патч)
        # Новая ревизия текущей джобы создана.
        revs = (
            (await s.execute(select(Revision).where(Revision.created_from_job_id == jid)))
            .scalars()
            .all()
        )
        assert len(revs) == 1
        assert revs[0].revision_no == 1
    assert (jid, JobState.BUILDING) in dispatched
    assert await _llm_usage_count(jid) == 1


# --- task_fix: невалидный патч = fix-неудача (retry_count НЕ растёт, остаёмся в FIXING) ---


async def test_fix_invalid_patch_stays_fixing_no_retry_increment(fixing_job, monkeypatch):
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    # Невалидное дерево (пустой files) → доменный schema-фейл. ADR-020 §I.3: structured-слой
    # РЕТРАИТ parse/schema-фейл до AGENT_OUTPUT_MAX_RETRIES (re-семплирование) ВНУТРИ шага
    # агента, прежде чем пробросить AgentOutputError в task. Подаём bad на все попытки.
    settings = get_settings()
    n_calls = settings.agent_output_max_retries + 1
    bad = json.dumps({"files": [], "entry": "x", "build": {"command": "vite build"}})
    _FakeClient._texts = [bad] * n_calls

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING  # остаёмся в FIXING
        assert job.retry_count == 0  # НЕ инкрементируется на невалидном патче
        # _handle_invalid_patch пометил новое событие классом agent_output_invalid.
        assert job.failure_event_pending is True
    # llm_usage записан ПОСЛЕ КАЖДОГО вызова (включая retry, §I.3): N вызовов = N записей.
    assert await _llm_usage_count(jid) == n_calls
    # re-dispatch task_fix (новый виток проверит гарды).
    assert (jid, JobState.FIXING) in dispatched


# --- task_fix: unrecoverable → FAILED(fixer_gave_up) ---


async def test_fix_unrecoverable_signal_fails_with_fixer_gave_up(fixing_job, monkeypatch):
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [
        json.dumps({"unrecoverable": True, "reason": "irreparable", "explanation": "broken"})
    ]

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "fixer_gave_up"
        events = (await s.execute(select(JobEvent).where(JobEvent.job_id == jid))).scalars().all()
        assert any(e.event_type == "fixer_gave_up" for e in events)


# --- Гарды на входе в FIXING → FAILED(reason) ---


async def test_fix_guard_hard_cap_fails_build_unrecoverable(fixing_job, monkeypatch):
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]  # не должен быть вызван
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        job.retry_count = 3
        job.max_fix_attempts = 3
        await s.commit()

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "build_unrecoverable"
    # Agent 4 НЕ вызывался (гард kill перед LLM).
    assert _FakeClient.captured == []
    assert await _llm_usage_count(jid) == 0


async def test_fix_guard_budget_fails_budget_exhausted(fixing_job, monkeypatch):
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        job.spend_usd = Decimal("5.0000")
        job.budget_usd = Decimal("5.0000")
        await s.commit()

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "budget_exhausted"
    assert _FakeClient.captured == []  # kill перед LLM-вызовом


async def test_fix_guard_wall_clock_fails_wall_clock_exceeded(fixing_job, monkeypatch):
    from datetime import UTC, datetime, timedelta

    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        job.wall_clock_deadline = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "wall_clock_exceeded"


# --- no-progress: реальный повтор сигнатуры на НОВОМ событии → FAILED(no_progress) ---


async def test_no_progress_same_signature_second_event_fails(fixing_job, monkeypatch):
    """Agent 4 пропатчил, но передеплой дал ровно ту же сигнатуру (новое событие,
    failure_event_pending=True, last_failure_signature == текущая) → no_progress."""
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]

    # Предзаполнить last_failure_signature = сигнатура текущего лога (как будто гард
    # уже видел её на прошлом витке), оставив failure_event_pending=True (новое событие).
    from app.pipeline.failure_signature import compute_failure_signature

    current_log = storage.objects[s3.build_log_key(jid)].decode()
    sig = compute_failure_signature(current_log)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        job.last_failure_signature = sig
        job.failure_event_pending = True
        await s.commit()

    await tasks._fix(jid)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "no_progress"
    assert _FakeClient.captured == []  # Agent 4 НЕ вызван — гард сработал


# --- crash-resume: тот же лог, флаг сброшен → resume (Agent 4 вызывается) ---


async def test_crash_resume_same_signature_no_pending_calls_agent4(fixing_job, monkeypatch):
    """Reconciler ре-диспетчеризовал task_fix по тому же failure_log; событие уже
    потреблено прошлым прогоном гарда (failure_event_pending=False) → resume, НЕ
    no_progress: Agent 4 вызывается, джоба продолжает цикл."""
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]

    from app.pipeline.failure_signature import compute_failure_signature

    current_log = storage.objects[s3.build_log_key(jid)].decode()
    sig = compute_failure_signature(current_log)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        job.last_failure_signature = sig
        job.failure_event_pending = False  # crash-resume: событие уже потреблено
        await s.commit()

    await tasks._fix(jid)

    # Agent 4 ВЫЗВАН (resume), джоба продвинулась в BUILDING — НЕ no_progress.
    assert len(_FakeClient.captured) == 1
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.BUILDING
        assert job.failure_reason != "no_progress"


# --- invalid-patch loop: agent_output_invalid дважды (новое событие) → no_progress ---


async def test_invalid_patch_loop_caught_by_no_progress(fixing_job, monkeypatch):
    """_handle_invalid_patch помечает новое событие классом agent_output_invalid.

    Семантика no-progress (ADR-005) — «та же сигнатура на ВТОРОМ distinct failure-event».
    Лог входа — build_error; первый невалидный патч переписывает лог на
    agent_output_invalid (это и есть первое distinct событие класса invalid). Повтор той
    же agent_output_invalid сигнатуры на СЛЕДУЮЩЕМ событии ловится гардом. Раскладка по
    виткам _fix:
      виток 1: вход build_error → гард пишет build_error-сигнатуру → Agent 4 invalid →
               лог переписан на agent_output_invalid (событие #1 класса invalid);
      виток 2: вход agent_output_invalid → сигнатура сдвинулась (build→invalid) → гард
               пропускает, пишет invalid-сигнатуру → Agent 4 invalid (событие #2);
      виток 3: вход та же agent_output_invalid сигнатура + новое событие → no_progress.
    """
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    bad = json.dumps({"files": [], "entry": "x", "build": {"command": "vite build"}})

    # Виток 1: build_error → Agent 4 invalid → лог переписан на agent_output_invalid.
    _FakeClient._texts = [bad]
    await tasks._fix(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING
        assert job.failure_event_pending is True

    # Виток 2: сигнатура сдвинулась build→invalid → гард пропускает, снова invalid.
    _FakeClient._texts = [bad]
    await tasks._fix(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING

    # Виток 3: та же agent_output_invalid сигнатура на новом событии → no_progress.
    _FakeClient._texts = [bad]
    await tasks._fix(jid)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "no_progress"
        assert job.retry_count == 0  # невалидные патчи не инкрементируют retry_count


# --- Agent 4 вход: дерево последней ревизии ТЕКУЩЕЙ джобы ---


async def test_agent4_input_uses_latest_revision_of_current_job(fixing_job, monkeypatch):
    """Вход Fixer = source.tgz ревизии, у которой created_from_job_id=job_id И
    revision_no=max среди ревизий ЭТОЙ джобы; НЕ глобальный max по проекту."""
    pid, jid, storage = fixing_job
    tasks, dispatched = _wire(monkeypatch, storage)
    _FakeClient._texts = [_valid_patch_json()]

    # Смоделируем: у проекта есть прежняя good-ревизия от ДРУГОЙ джобы с большим
    # revision_no (как в edit-цикле S5), и ревизия-кандидат текущей джобы с меньшим no.
    other_jid = new_job_id()
    cand_ref = s3.source_key(jid) + ".candidate"
    storage.objects[cand_ref] = _src_tgz(extra="// CANDIDATE_OF_CURRENT_JOB")
    other_ref = "sources/other/source.tgz"
    storage.objects[other_ref] = _src_tgz(extra="// OTHER_JOB_HIGHER_REVNO")
    async with session_scope() as s:
        # other job (для FK created_from_job_id).
        s.add(
            GenerationJob(
                id=other_jid,
                project_id=pid,
                user_id=UID,
                state=JobState.LIVE,
                kind="generation",
            )
        )
        await s.flush()
        # Ревизия-кандидат текущей джобы (revision_no=1).
        s.add(
            Revision(
                id=new_revision_id(),
                project_id=pid,
                revision_no=1,
                source_artifact_ref=cand_ref,
                created_from_job_id=jid,
                is_good=False,
            )
        )
        # Прежняя good-ревизия другой джобы с БОЛЬШИМ revision_no.
        s.add(
            Revision(
                id=new_revision_id(),
                project_id=pid,
                revision_no=2,
                source_artifact_ref=other_ref,
                created_from_job_id=other_jid,
                is_good=True,
            )
        )
        await s.commit()

    await tasks._fix(jid)

    # Подан кандидат текущей джобы (revision_no=1), НЕ глобальный max (revision_no=2).
    assert len(_FakeClient.captured) == 1
    assert "CANDIDATE_OF_CURRENT_JOB" in _FakeClient.captured[0]
    assert "OTHER_JOB_HIGHER_REVNO" not in _FakeClient.captured[0]
