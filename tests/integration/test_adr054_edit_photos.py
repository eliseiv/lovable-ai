"""Integration: фото, приложенные к правке, доходят до сайта (ADR-054).

Уровень `_edit` с реальным Postgres: editor (мок) получает пути новых фото в нужном порядке,
а ссылки в его дереве сервер чинит до упаковки — выдуманное имя заменяется реальным фото,
лишний префикс убирается, отчёт пишется в job_events.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.enums import JobState
from app.db.models import Attachment, GenerationJob
from app.db.session import session_scope
from app.pipeline.agents.agent4 import Agent4Result
from app.schemas.agent_output import ValidatedFile, ValidatedTree
from app.storage import s3
from app.workers import tasks as worker_tasks
from tests.integration.test_editing_state_pipeline import (
    UID,
    _events,
    _purge,
    _seed_edit_job,
    edit_env,  # noqa: F401 — фикстура переиспользуется
)

pytestmark = pytest.mark.asyncio


async def _purge_attachments() -> None:
    async with session_scope() as s:
        job_ids = select(GenerationJob.id).where(GenerationJob.user_id == UID)
        await s.execute(delete(Attachment).where(Attachment.job_id.in_(job_ids)))
        await s.commit()


@pytest_asyncio.fixture
async def photos_env(edit_env):  # noqa: ANN001, ANN201, F811
    yield edit_env
    await _purge_attachments()
    await _purge()


async def _add_photo(storage, *, att_id: str, project_id: str, job_id: str, mime: str) -> None:  # noqa: ANN001
    key = f"uploads/{project_id}/{att_id}"
    storage.objects[key] = b"\x89PNG-bytes-" + att_id.encode()
    async with session_scope() as s:
        s.add(
            Attachment(
                id=att_id,
                project_id=project_id,
                job_id=job_id,
                s3_ref=key,
                filename=f"{att_id}.orig",
                mime=mime,
                size_bytes=16,
            )
        )
        await s.commit()


def _tree(html: str, css: str) -> ValidatedTree:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    return ValidatedTree(
        files=(
            ValidatedFile(path="index.html", encoding="utf8", content_bytes=html.encode()),
            ValidatedFile(path="src/style.css", encoding="utf8", content_bytes=css.encode()),
            ValidatedFile(path="package.json", encoding="utf8", content_bytes=pkg.encode()),
        ),
        entry="index.html",
        build_command="vite build",
        build_output_dir="dist",
    )


def _unpack(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            if extracted is not None:
                out[member.name] = extracted.read()
    return out


async def test_edit_photos_reach_the_site(photos_env, monkeypatch):
    data = await _seed_edit_job(JobState.CREATED)
    storage = photos_env["storage"]
    async with session_scope() as s:
        gen_job_id = (
            await s.execute(
                select(GenerationJob.id).where(
                    GenerationJob.project_id == data["pid"], GenerationJob.kind == "generation"
                )
            )
        ).scalar_one()
    # Фото генерации (раньше) и два фото этой правки.
    await _add_photo(
        storage, att_id="att_old00001", project_id=data["pid"], job_id=gen_job_id, mime="image/jpeg"
    )
    await _add_photo(
        storage,
        att_id="att_new00001",
        project_id=data["pid"],
        job_id=data["edit_jid"],
        mime="image/png",
    )
    await _add_photo(
        storage,
        att_id="att_new00002",
        project_id=data["pid"],
        job_id=data["edit_jid"],
        mime="image/png",
    )

    seen: dict[str, object] = {}

    async def _editor(settings, **kwargs):  # noqa: ANN001, ANN202
        await kwargs["before_call"]()
        seen["new"] = [e.rel_path for e in kwargs["new_assets"]]
        seen["project"] = [e.rel_path for e in kwargs["project_assets"]]
        seen["images"] = len(kwargs["images"])
        # Модель ошиблась так, как это бывало: выдумала имя и приписала public/.
        return Agent4Result(
            call=None,
            tree=_tree(
                html='<img src="uploads/photo1.png"><img src="public/uploads/att_new00002.png">',
                css=".hero{background:url(uploads/att_old00001.jpg)}",
            ),
            unrecoverable=None,
        )

    monkeypatch.setattr(worker_tasks, "run_agent4_editor", _editor)

    await worker_tasks._edit(data["edit_jid"])

    # Editor знал пути новых фото — в том же порядке, что и изображения.
    assert seen["new"] == ["uploads/att_new00001.png", "uploads/att_new00002.png"]
    assert seen["images"] == 2
    assert "uploads/att_old00001.jpg" in seen["project"]

    files = _unpack(storage.objects[s3.source_key(data["edit_jid"])])
    html = files["index.html"].decode()
    assert '<img src="uploads/att_new00001.png">' in html
    assert '<img src="uploads/att_new00002.png">' in html
    assert "url(/uploads/att_old00001.jpg)" in files["src/style.css"].decode()
    # Сами файлы на месте — все три фото проекта.
    for name in ("att_old00001.jpg", "att_new00001.png", "att_new00002.png"):
        assert f"public/uploads/{name}" in files

    [report] = await _events(data["edit_jid"], "upload_refs_checked")
    assert report.payload["renamed"] == {"photo1.png": "att_new00001.png"}
    assert report.payload["unused_new"] == []

    async with session_scope() as s:
        job = await s.get(GenerationJob, data["edit_jid"])
        assert job.state == JobState.BUILDING


async def test_edit_without_photos_writes_no_report(photos_env):
    data = await _seed_edit_job(JobState.CREATED)

    await worker_tasks._edit(data["edit_jid"])

    assert await _events(data["edit_jid"], "upload_refs_checked") == []
