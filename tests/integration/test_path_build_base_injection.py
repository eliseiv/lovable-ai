"""Integration: воркер передаёт в sandbox build-команду с `--base=/s/{site_id}/` (ADR-017 §Fix).

Нормативный источник — docs/06-testing-strategy.md → «Vite base-path (нормативно)» / ADR-017
§Fix 2026-06-08. Реальной Vite-сборки в тесте нет (нет Node/Docker), поэтому проверяем
ближайший автоматизируемый инвариант: команда, доходящая до sandbox.run_build в режиме `path`,
содержит `--base=/s/{site_id}/`, инжектированный В ТОКЕН `npx vite build` (а НЕ в хвост строки).
Без base ассеты резолвятся в `/assets/...` → 404 за StripPrefix → пустой экран (прод-инцидент).

Прогоняет настоящую task-функцию `_build_request` сквозь pipeline нормализации+инжекта
(read_build_manifest → augment_build_command → sandbox.run_build). Postgres реальный; S3/sandbox/
docker/health — моки. site_id берётся из реального _resolve_site_id (персист в job_events).
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, SiteDeployment, User
from app.db.session import session_scope
from app.storage import s3
from app.workers.tasks import _SITE_ID_ASSIGNED_EVENT

pytestmark = pytest.mark.asyncio

UID = "u_basepathowner0000000"


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


def _src_tgz(build_command: str) -> bytes:
    """source.tgz с .build.json-манифестом, несущим заданную build.command."""
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    manifest = json.dumps({"command": build_command, "output_dir": "dist"})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        members = {
            ".build.json": manifest.encode(),
            "index.html": b"<html></html>",
            "package.json": pkg.encode(),
        }
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _purge(uid: str) -> None:
    from sqlalchemy import delete, select

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
        if pids:
            await s.execute(delete(SiteDeployment).where(SiteDeployment.project_id.in_(pids)))
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


@pytest.fixture
async def building_job(autonomous_db):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    await _purge(UID)
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("basepath-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=UID, prompt="path base test", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=JobState.BUILDING,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
            )
        )
        await s.commit()
    yield pid, jid
    await _purge(UID)


async def _run_build_capture_command(monkeypatch, jid: str, build_command: str) -> str:
    """Прогоняет _build_request в path-режиме, возвращает команду, дошедшую до sandbox.run_build."""
    import app.deploy.sandbox as sandbox
    import app.pipeline.events as events
    import app.workers.tasks as tasks
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "site_routing_mode", "path", raising=False)
    monkeypatch.setattr(settings, "apps_domain", "apps.domain", raising=False)

    storage = _FakeStorage()
    await storage.put_bytes(s3.source_key(jid), _src_tgz(build_command))
    monkeypatch.setattr(tasks, "get_storage", lambda: storage)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop_publish)
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)

    captured: dict[str, str] = {}

    def _fake_run_build(s, ws, command, output_dir):  # noqa: ANN001, ANN202
        captured["command"] = command
        dist = ws / output_dir
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_bytes(b"<html></html>")
        return sandbox.BuildResult(success=True, log="ok", dist_dir=dist)

    monkeypatch.setattr(tasks.sandbox, "run_build", _fake_run_build)

    await tasks._build_request(jid)
    assert "command" in captured, "sandbox.run_build не был вызван — build не дошёл до сборки"
    return captured["command"]


async def test_path_build_command_has_base_injected_into_token(building_job, monkeypatch):
    """Нормализация + инжект: `npm install && npm run build` → `npx vite build --base=/s/{id}/`."""
    pid, jid = building_job
    command = await _run_build_capture_command(monkeypatch, jid, "npm install && npm run build")

    # site_id, присвоенный воркером (job_events), — тот же, что в --base.
    async with session_scope() as s:
        from sqlalchemy import select

        ev = (
            await s.execute(
                select(JobEvent).where(
                    JobEvent.job_id == jid,
                    JobEvent.event_type == _SITE_ID_ASSIGNED_EVENT,
                )
            )
        ).scalar_one()
        site_id = ev.payload["site_id"] if isinstance(ev.payload, dict) else None
    assert site_id and re.fullmatch(r"[a-z0-9]{16}", site_id)

    # base инжектирован В ТОКЕН (сразу после `npx vite build`), а НЕ в хвост строки.
    assert f"npx vite build --base=/s/{site_id}/" in command
    assert command == f"npm install && npx vite build --base=/s/{site_id}/"
    # Голого `npm run build`/`vite build`/`npm ci` в итоговой команде нет.
    assert "npm run build" not in command
    assert "npm ci" not in command


async def test_path_build_command_already_canonical_gets_base(building_job, monkeypatch):
    """Уже каноническая `npm install && npx vite build` → получает `--base` в токен."""
    pid, jid = building_job
    command = await _run_build_capture_command(monkeypatch, jid, "npm install && npx vite build")
    assert re.search(r"npx vite build --base=/s/[a-z0-9]{16}/$", command), command
    assert command.count("--base") == 1
