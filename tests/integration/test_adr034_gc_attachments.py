"""Integration: ADR-034 §D7 — GC префикса uploads/{project_id}/ + строк attachments.

Реальный Postgres (autonomous_db / session_scope). S3 — in-memory fake (delete_prefix),
docker/host-volume замоканы. Attachments сидируются прямо в БД (приём-путь тестируется
отдельно; здесь — GC-сторона §D7).

Источник истины: docs/adr/ADR-034 §D7, docs/06-testing-strategy.md §Integration «GC префикса».

Покрывает сценарий 8 ТЗ:
- project.gc удаляет S3-префикс uploads/{project_id}/ (отдельным delete_prefix) и строки
  attachments проекта; FK-порядок hard-delete (attachments ДО generation_jobs/projects) не
  нарушается; повторный GC идемпотентен (no-op).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.ids import new_attachment_id, new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import Attachment, GenerationJob, JobEvent, Project, User
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio

UID = "u_gc034owner00000000001"


class _FakeStorage:
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
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        pids = (
            (await s.execute(select(GenerationJob.project_id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        all_pids = set(pids) | set(
            (await s.execute(select(Project.id).where(Project.user_id == uid))).scalars().all()
        )
        # attachments ДО generation_jobs/projects (FK).
        if all_pids:
            await s.execute(delete(Attachment).where(Attachment.project_id.in_(all_pids)))
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in all_pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


@pytest_asyncio.fixture
async def gc_project_with_attachments(autonomous_db):  # noqa: ANN001, ANN201
    """Committed soft-deleted проект с 2 джобами и 3 attachments-строками (скоуп project_id)."""
    await _purge(UID)
    pid = new_project_id()
    job_gen = new_job_id()
    job_edit = new_job_id()
    att_ids = [new_attachment_id() for _ in range(3)]

    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("gc034-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="p", title=None))
        s.add(
            GenerationJob(
                id=job_gen, project_id=pid, user_id=UID, state=JobState.LIVE, kind="generation"
            )
        )
        s.add(
            GenerationJob(
                id=job_edit, project_id=pid, user_id=UID, state=JobState.LIVE, kind="edit"
            )
        )
        await s.flush()
        # 2 фото с генерации + 1 с правки (все скоупятся project_id).
        for i, att_id in enumerate(att_ids):
            jid = job_gen if i < 2 else job_edit
            s.add(
                Attachment(
                    id=att_id,
                    project_id=pid,
                    job_id=jid,
                    s3_ref=f"uploads/{pid}/{att_id}.png",
                    filename=f"f{i}.png",
                    mime="image/png",
                    size_bytes=100,
                    width=10,
                    height=10,
                    sha256="x" * 64,
                )
            )
        proj = await s.get(Project, pid)
        proj.deleted_at = datetime.now(UTC)
        await s.commit()

    yield {"pid": pid, "job_gen": job_gen, "job_edit": job_edit, "att_ids": att_ids}
    await _purge(UID)


def _wire_gc(monkeypatch, storage):  # noqa: ANN001, ANN202
    import app.deploy.project_gc as gc

    monkeypatch.setattr(gc, "get_storage", lambda: storage)
    monkeypatch.setattr(gc.docker_deploy, "teardown_container", lambda name: None)
    monkeypatch.setattr(gc.shutil, "rmtree", lambda path, ignore_errors=False: None)


async def test_gc_deletes_uploads_prefix_and_attachment_rows(
    gc_project_with_attachments, monkeypatch
):
    from app.deploy.project_gc import _run_gc

    ctx = gc_project_with_attachments
    pid = ctx["pid"]

    storage = _FakeStorage()
    # Сидируем uploads/{pid}/ объекты + сосед-проект с общим строковым началом (изоляция).
    for att_id in ctx["att_ids"]:
        storage.objects[f"uploads/{pid}/{att_id}.png"] = b"img"
    neighbor_key = f"uploads/{pid}_neighbor/zzz.png"
    storage.objects[neighbor_key] = b"other"

    _wire_gc(monkeypatch, storage)
    await _run_gc(pid)

    # §D7: отдельный delete_prefix по project-scoped uploads/{pid}/ (со слэшем — нет захвата).
    upload_prefixes = [c[0] for c in storage.delete_prefix_calls if c[0].startswith("uploads/")]
    assert f"uploads/{pid}/" in upload_prefixes
    # Объекты проекта удалены, сосед с общим началом — НЕ затронут (точный префикс со слэшем).
    assert not any(k.startswith(f"uploads/{pid}/") for k in storage.objects)
    assert neighbor_key in storage.objects

    # Строки attachments проекта удалены (вместе с generation_jobs/projects, FK-порядок соблюдён).
    async with session_scope() as s:
        cnt = await s.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.project_id == pid)
        )
        assert cnt == 0
        assert await s.get(Project, pid) is None
        assert await s.get(GenerationJob, ctx["job_gen"]) is None


async def test_gc_idempotent_repeat_no_op(gc_project_with_attachments, monkeypatch):
    """Повторный project.gc на уже-вычищенном проекте — no-op (идемпотентность §D7)."""
    from app.deploy.project_gc import _run_gc

    pid = gc_project_with_attachments["pid"]
    storage = _FakeStorage()
    for att_id in gc_project_with_attachments["att_ids"]:
        storage.objects[f"uploads/{pid}/{att_id}.png"] = b"img"
    _wire_gc(monkeypatch, storage)

    await _run_gc(pid)
    # Второй прогон не падает и ничего не находит (строка проекта уже удалена).
    await _run_gc(pid)

    async with session_scope() as s:
        assert await s.get(Project, pid) is None
        cnt = await s.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.project_id == pid)
        )
        assert cnt == 0
