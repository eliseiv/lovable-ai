"""Integration: ADR-034 §D2/§D4/§D9/§D11 — multipart-приём изображений + идемпотентность.

Реальный Postgres (client шарит тест-сессию). dispatch/publish замоканы (no_side_effects);
S3 — in-memory fake (put_bytes/get_bytes), внедряется в project_service/edit_service.get_storage.

Источник истины: docs/adr/ADR-034 §D2/§D4/§D9/§D11, docs/modules/api/02-api-contracts.md
(POST /projects · /edits multipart), docs/06-testing-strategy.md §Integration «multipart-приём»
+ «идемпотентность приёма».

Покрывает сценарии ТЗ:
- 1 (contract multipart): POST /projects и /edits как multipart → 202; строки attachments
  созданы (project_id/job_id/s3_ref/mime/size_bytes); S3-объект под uploads/{project_id}/...;
  без images — текстовый путь без attachments/S3 (нет регрессий);
- 2 (валидация на уровне endpoint): подложенный .png с не-image содержимым → 422
  unsupported_image_type (тип по magic bytes);
- 3 (идемпотентность приёма D9): replay того же Idempotency-Key НЕ создаёт повторных строк
  attachments / S3-объектов.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.ids import new_job_id, new_project_id, new_revision_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import Attachment, GenerationJob, Project, Revision, User
from app.services import edit_service, project_service
from tests.support import images as I

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """In-memory S3 fake: put_bytes/get_bytes (как S3Storage). Хранит objects/content_types."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.put_calls: list[str] = []

    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self.put_calls.append(key)
        self.objects[key] = data
        self.content_types[key] = content_type
        return key

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


@pytest.fixture
def fake_storage(monkeypatch):  # noqa: ANN001, ANN201
    """Внедряет in-memory S3 fake в project_service и edit_service (точка persist_images)."""
    storage = _FakeStorage()
    monkeypatch.setattr(project_service, "get_storage", lambda: storage)
    monkeypatch.setattr(edit_service, "get_storage", lambda: storage)
    return storage


def _img_file(name: str, data: bytes, content_type: str = "image/png"):  # noqa: ANN202
    return ("images", (name, data, content_type))


# --- сценарий 1: POST /projects multipart с images → 202 + attachments + S3 ---


async def test_create_project_with_images_persists_attachments_and_s3(
    client, auth_headers, session, seeded_user, no_side_effects, fake_storage
):
    png = I.png_bytes(10, 8)
    gif = I.gif_bytes(6, 4)
    resp = await client.post(
        "/v1/projects",
        data={"prompt": "cafe site"},
        files=[_img_file("logo.png", png), _img_file("hero.gif", gif, "image/gif")],
        headers={**auth_headers, "Idempotency-Key": "img-create-1"},
    )
    assert resp.status_code == 202, resp.text
    pid = resp.json()["project_id"]
    jid = resp.json()["job_id"]

    rows = (
        (await session.execute(select(Attachment).where(Attachment.project_id == pid)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    by_mime = {r.mime: r for r in rows}
    assert set(by_mime) == {"image/png", "image/gif"}
    png_row = by_mime["image/png"]
    # Строки несут project_id/job_id/s3_ref/size_bytes (§D6).
    assert png_row.project_id == pid
    assert png_row.job_id == jid
    assert png_row.size_bytes == len(png)
    assert png_row.width == 10 and png_row.height == 8
    # S3-ключ детерминирован uploads/{project_id}/{att_id}.{ext} (§D7).
    assert png_row.s3_ref == f"uploads/{pid}/{png_row.id}.png"
    assert png_row.s3_ref in fake_storage.objects
    assert fake_storage.objects[png_row.s3_ref] == png
    assert fake_storage.content_types[png_row.s3_ref] == "image/png"


async def test_create_project_without_images_no_attachments_no_s3(
    client, auth_headers, session, seeded_user, no_side_effects, fake_storage
):
    """Запрос без images → прежнее поведение: 202, ноль строк attachments / S3-объектов."""
    resp = await client.post(
        "/v1/projects",
        data={"prompt": "text only"},
        headers={**auth_headers, "Idempotency-Key": "no-img-1"},
    )
    assert resp.status_code == 202
    pid = resp.json()["project_id"]
    count = await session.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.project_id == pid)
    )
    assert count == 0
    assert fake_storage.put_calls == []


async def test_create_project_bad_magic_bytes_422_unsupported(
    client, auth_headers, seeded_user, no_side_effects, fake_storage
):
    """Подложенный .png с не-image содержимым → 422 unsupported_image_type (тип по magic bytes)."""
    resp = await client.post(
        "/v1/projects",
        data={"prompt": "evil"},
        files=[_img_file("fake.png", b"not really a png at all")],
        headers={**auth_headers, "Idempotency-Key": "bad-magic-1"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["reason"] == "unsupported_image_type"
    # Ни одного S3-объекта (валидация ДО persist).
    assert fake_storage.put_calls == []


# --- сценарий 3: идемпотентность приёма (D9) ---


async def test_idempotent_replay_does_not_duplicate_attachments_or_s3(
    client, auth_headers, session, seeded_user, no_side_effects, fake_storage
):
    """Replay того же Idempotency-Key с теми же файлами → НЕ дублирует attachments/S3 (§D9)."""
    png = I.png_bytes(8, 8)
    headers = {**auth_headers, "Idempotency-Key": "img-idem-1"}

    r1 = await client.post(
        "/v1/projects", data={"prompt": "p"}, files=[_img_file("a.png", png)], headers=headers
    )
    assert r1.status_code == 202
    pid = r1.json()["project_id"]
    s3_keys_after_first = set(fake_storage.objects)
    put_count_after_first = len(fake_storage.put_calls)

    # Replay: тот же ключ → та же джоба, повторных строк/объектов нет.
    r2 = await client.post(
        "/v1/projects", data={"prompt": "p"}, files=[_img_file("a.png", png)], headers=headers
    )
    assert r2.status_code == 202
    assert r2.json()["project_id"] == pid
    assert r2.json()["job_id"] == r1.json()["job_id"]

    count = await session.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.project_id == pid)
    )
    assert count == 1, "replay не создаёт второй строки attachments"
    assert set(fake_storage.objects) == s3_keys_after_first, "replay не пишет новый S3-объект"
    assert len(fake_storage.put_calls) == put_count_after_first


# --- сценарий 1 (edits): POST /edits multipart с images ---


async def _live_project_with_good_revision(session, uid) -> str:  # noqa: ANN001
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
    await session.flush()
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


@pytest_asyncio.fixture
async def _no_edit_dispatch(monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setattr(edit_service, "dispatch_for_state", lambda *a, **k: None)


async def test_create_edit_with_images_persists_attachments_scoped_project(
    client, session, fake_storage, _no_edit_dispatch
):
    """POST /edits multipart с images → 202; строки attachments скоупятся project_id (§D4)."""
    uid = "u_adr034_edit0000001"
    session.add(
        User(
            id=uid,
            api_key_hash=hash_api_key("adr034-edit-key"),
            monthly_budget_usd=Decimal("50.0000"),
            status="active",
        )
    )
    await session.flush()
    pid = await _live_project_with_good_revision(session, uid)

    webp = I.webp_vp8_bytes(20, 15)
    resp = await client.post(
        f"/v1/projects/{pid}/edits",
        data={"instruction": "add the new photo"},
        files=[_img_file("new.webp", webp, "image/webp")],
        headers={"Authorization": "Bearer adr034-edit-key", "Idempotency-Key": "edit-img-1"},
    )
    assert resp.status_code == 202, resp.text
    jid = resp.json()["job_id"]

    rows = (
        (await session.execute(select(Attachment).where(Attachment.project_id == pid)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].mime == "image/webp"
    assert rows[0].project_id == pid  # скоуп project_id (не джобы)
    assert rows[0].job_id == jid  # аудит: на какой джобе пришёл
    assert rows[0].s3_ref == f"uploads/{pid}/{rows[0].id}.webp"
    assert rows[0].s3_ref in fake_storage.objects
