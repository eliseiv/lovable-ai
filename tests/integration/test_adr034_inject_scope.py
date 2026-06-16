"""Integration: ADR-034 §D4/§D5 — скоуп инжекта project_id (фото генерации живут на правке).

Реальный Postgres (session fixture). Проверяет, что инжект/манифест берут ВСЕ фото проекта
(не только текущей джобы) и собирают детерминированные серверные пути.

Источник истины: docs/adr/ADR-034 §D4/§D5, docs/06-testing-strategy.md §Integration
«детерминированный инжект» (скоуп project_id, фото с генерации доступно на правке).

Покрывает интеграционную часть сценария 6/7 ТЗ:
- list_project_attachments возвращает ВСЕ фото проекта (с генерации И с правки), детерминированно;
- _injected_assets → public/uploads/{att_id}.{ext} с байтами из S3;
- _asset_manifest_entries → ОДНА относительная форма uploads/{att_id}.{ext}.
"""

from __future__ import annotations

from app.core.ids import new_attachment_id, new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import Attachment, GenerationJob, Project
from app.services.attachments_service import list_project_attachments
from app.workers.tasks import (
    _asset_manifest_entries,
    _injected_assets,
    _load_vision_images,
)


class _FakeStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


async def _seed_project_with_two_jobs_attachments(session, uid: str):  # noqa: ANN001, ANN202
    """Проект с gen-джобой (2 фото) и edit-джобой (1 фото) — 3 фото скоупятся project_id."""
    pid = new_project_id()
    job_gen = new_job_id()
    job_edit = new_job_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    session.add(
        GenerationJob(
            id=job_gen, project_id=pid, user_id=uid, state=JobState.LIVE, kind="generation"
        )
    )
    session.add(
        GenerationJob(id=job_edit, project_id=pid, user_id=uid, state=JobState.LIVE, kind="edit")
    )
    await session.flush()
    atts = []
    specs = [
        (job_gen, "image/png", "png"),
        (job_gen, "image/gif", "gif"),
        (job_edit, "image/webp", "webp"),
    ]
    for jid, mime, ext in specs:
        att_id = new_attachment_id()
        session.add(
            Attachment(
                id=att_id,
                project_id=pid,
                job_id=jid,
                s3_ref=f"uploads/{pid}/{att_id}.{ext}",
                filename=f"{att_id}.{ext}",
                mime=mime,
                size_bytes=10,
                width=4,
                height=4,
                sha256="x" * 64,
            )
        )
        atts.append((att_id, mime, ext, jid))
    await session.flush()
    return pid, job_gen, job_edit, atts


async def test_list_project_attachments_returns_all_project_photos(session, seeded_user):
    """Скоуп project_id: фото генерации + фото правки — все возвращаются (не теряются на правке)."""
    pid, job_gen, job_edit, atts = await _seed_project_with_two_jobs_attachments(
        session, seeded_user.id
    )
    rows = await list_project_attachments(session, pid)
    assert len(rows) == 3
    # Включает фото обеих джоб (генерации и правки).
    job_ids = {r.job_id for r in rows}
    assert job_ids == {job_gen, job_edit}


async def test_injected_assets_build_public_uploads_paths_with_s3_bytes(session, seeded_user):
    """_injected_assets → public/uploads/{att_id}.{ext} с реальными байтами из S3 (§D4)."""
    pid, _, _, atts = await _seed_project_with_two_jobs_attachments(session, seeded_user.id)
    storage = _FakeStorage(
        {f"uploads/{pid}/{a}.{e}": f"bytes-{a}".encode() for a, _m, e, _j in atts}
    )

    rows = await list_project_attachments(session, pid)
    injected = await _injected_assets(storage, rows)

    paths = {asset.server_path for asset in injected}
    expected = {f"public/uploads/{a}.{e}" for a, _m, e, _j in atts}
    assert paths == expected
    # Байты соответствуют S3-объекту.
    for asset in injected:
        att_id = asset.server_path.split("/")[-1].split(".")[0]
        assert asset.data == f"bytes-{att_id}".encode()


async def test_asset_manifest_entries_relative_form(session, seeded_user):
    """_asset_manifest_entries → uploads/{att_id}.{ext} (БЕЗ public/, БЕЗ ведущего /) (§D5)."""
    pid, _, _, atts = await _seed_project_with_two_jobs_attachments(session, seeded_user.id)
    rows = await list_project_attachments(session, pid)
    entries = _asset_manifest_entries(rows)
    rel_paths = {e.rel_path for e in entries}
    expected = {f"uploads/{a}.{e}" for a, _m, e, _j in atts}
    assert rel_paths == expected
    assert all(not p.startswith("public/") and not p.startswith("/") for p in rel_paths)


async def test_load_vision_images_media_type_from_sniffed_mime(session, seeded_user):
    """_load_vision_images: media_type = attachments.mime (sniffed), байты из S3 (§D3)."""
    pid, _, _, atts = await _seed_project_with_two_jobs_attachments(session, seeded_user.id)
    storage = _FakeStorage({f"uploads/{pid}/{a}.{e}": b"img" for a, _m, e, _j in atts})
    rows = await list_project_attachments(session, pid)
    vision = await _load_vision_images(storage, rows)
    assert {v.media_type for v in vision} == {"image/png", "image/gif", "image/webp"}
    assert all(v.data == b"img" for v in vision)
