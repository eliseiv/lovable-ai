"""Integration: план сайта, стоимость генерации и выгрузка исходников (ADR-045/046).

Покрытие:
  - `GET /jobs/{jid}/plan` — секции по порядку со статусами; пустой план у задачи без плана;
  - `cost_usd` в `GET /jobs/{jid}` — фактический расход задачи;
  - `avg_generation_cost_usd` в `GET /billing/me` — среднее по успешным генерациям, `null`
    при отсутствии данных;
  - `GET /projects/{pid}/source` — zip исходников текущей и указанной ревизии, служебный
    `.build.json` в архив не попадает; чужой проект и проект без ревизий → 404.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobSection, Project, Revision, User
from app.deploy import workspace
from app.core.config import get_settings
from app.schemas.agent_output import validate_agent_output

pytestmark = pytest.mark.asyncio

_UID = "u_plan_cost_src01"
_OTHER_UID = "u_plan_other0001"


async def _user(session, uid: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(f"{uid}-legacy-key"),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def _project_with_job(
    session,  # noqa: ANN001
    *,
    uid: str = _UID,
    state: JobState = JobState.LIVE,
    spend: str = "0.0000",
    kind: str = "generation",
) -> tuple[str, str]:
    pid, jid = new_project_id(), new_job_id()
    session.add(Project(id=pid, user_id=uid, prompt="build me a site", title=None))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=uid,
            state=state,
            kind=kind,
            budget_usd=Decimal("5.0000"),
            spend_usd=Decimal(spend),
        )
    )
    await session.flush()
    return pid, jid


def _auth(uid: str = _UID) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}-legacy-key"}


# ============================ план сайта ============================


async def test_job_plan_returns_sections_in_order_with_status(client, session):
    await _user(session, _UID)
    _pid, jid = await _project_with_job(session)
    session.add_all(
        [
            JobSection(
                job_id=jid, section_id="hero", title="Главный экран", position=0, status="done"
            ),
            JobSection(
                job_id=jid, section_id="gallery", title="Галерея", position=1, status="pending"
            ),
        ]
    )
    await session.flush()

    resp = await client.get(f"/v1/jobs/{jid}/plan", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["sections"] == [
        {"id": "hero", "title": "Главный экран", "status": "done"},
        {"id": "gallery", "title": "Галерея", "status": "pending"},
    ]


async def test_job_plan_is_empty_when_no_plan(client, session):
    await _user(session, _UID)
    _pid, jid = await _project_with_job(session)

    resp = await client.get(f"/v1/jobs/{jid}/plan", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["sections"] == []


async def test_job_plan_of_foreign_job_is_404(client, session):
    await _user(session, _UID)
    await _user(session, _OTHER_UID)
    _pid, jid = await _project_with_job(session, uid=_OTHER_UID)

    resp = await client.get(f"/v1/jobs/{jid}/plan", headers=_auth())
    assert resp.status_code == 404


# ============================ стоимость ============================


async def test_job_status_exposes_cost(client, session):
    await _user(session, _UID)
    _pid, jid = await _project_with_job(session, spend="0.1234")

    resp = await client.get(f"/v1/jobs/{jid}", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["cost_usd"] == pytest.approx(0.1234)


async def test_billing_me_avg_generation_cost(client, session):
    user = await _user(session, _UID)
    await _project_with_job(session, spend="0.1000")
    await _project_with_job(session, spend="0.3000")
    # Не считаются: правка, незавершённая генерация и нулевой расход.
    await _project_with_job(session, spend="9.0000", kind="edit")
    await _project_with_job(session, spend="9.0000", state=JobState.BUILDING)
    await _project_with_job(session, spend="0.0000")
    assert user is not None

    resp = await client.get("/v1/billing/me", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["avg_generation_cost_usd"] == pytest.approx(0.2)


async def test_billing_me_avg_cost_is_null_without_data(client, session):
    await _user(session, _UID)

    resp = await client.get("/v1/billing/me", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["avg_generation_cost_usd"] is None


async def test_billing_me_avg_cost_ignores_old_jobs(client, session):
    await _user(session, _UID)
    _pid, jid = await _project_with_job(session, spend="0.5000")
    job = await session.get(GenerationJob, jid)
    assert job is not None
    job.created_at = datetime.now(UTC) - timedelta(days=60)
    await session.flush()

    resp = await client.get("/v1/billing/me", headers=_auth())
    assert resp.json()["avg_generation_cost_usd"] is None


# ============================ исходники ============================


def _source_tgz() -> bytes:
    """Валидное дерево Agent 3, упакованное как ревизия проекта."""
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    tree = validate_agent_output(
        {
            "files": [
                {"path": "package.json", "encoding": "utf8", "content": pkg},
                {
                    "path": "index.html",
                    "encoding": "utf8",
                    "content": "<!doctype html><html></html>",
                },
            ],
            "entry": "index.html",
            "build": {"tool": "vite", "command": "npm ci && vite build", "output_dir": "dist"},
        },
        get_settings(),
    )
    return workspace.pack_source_tgz(tree)


async def _project_with_revision(session, storage_stub) -> tuple[str, str]:  # noqa: ANN001
    pid, jid = await _project_with_job(session)
    ref = f"sources/{jid}/source.tgz"
    storage_stub[ref] = _source_tgz()
    rev = Revision(
        id=f"rev_{jid}",
        project_id=pid,
        revision_no=1,
        source_artifact_ref=ref,
        created_from_job_id=jid,
        is_good=True,
    )
    session.add(rev)
    await session.flush()
    project = await session.get(Project, pid)
    assert project is not None
    project.current_revision_id = rev.id
    await session.flush()
    return pid, ref


@pytest.fixture
def storage_stub(monkeypatch):  # noqa: ANN201
    """Подменяет S3 на словарь ref → bytes (эндпоинт читает артефакт, а не ходит в сеть)."""
    objects: dict[str, bytes] = {}

    class _Stub:
        async def get_bytes(self, key: str) -> bytes:
            return objects[key]

    monkeypatch.setattr("app.services.project_service.get_storage", lambda: _Stub())
    return objects


async def test_download_source_returns_zip_of_current_revision(client, session, storage_stub):
    await _user(session, _UID)
    pid, _ref = await _project_with_revision(session, storage_stub)

    resp = await client.get(f"/v1/projects/{pid}/source", headers=_auth())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert f"{pid}-rev1.zip" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["index.html", "package.json"]
        assert b"vite" in zf.read("package.json")


async def test_download_source_excludes_build_manifest(client, session, storage_stub):
    await _user(session, _UID)
    pid, _ref = await _project_with_revision(session, storage_stub)

    resp = await client.get(f"/v1/projects/{pid}/source", headers=_auth())
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert ".build.json" not in zf.namelist()


async def test_download_source_by_revision_no(client, session, storage_stub):
    await _user(session, _UID)
    pid, _ref = await _project_with_revision(session, storage_stub)

    resp = await client.get(f"/v1/projects/{pid}/source?revision_no=1", headers=_auth())
    assert resp.status_code == 200
    resp_missing = await client.get(f"/v1/projects/{pid}/source?revision_no=7", headers=_auth())
    assert resp_missing.status_code == 404


async def test_download_source_without_revision_is_404(client, session, storage_stub):
    await _user(session, _UID)
    pid, _jid = await _project_with_job(session)

    resp = await client.get(f"/v1/projects/{pid}/source", headers=_auth())
    assert resp.status_code == 404


async def test_download_source_of_foreign_project_is_404(client, session, storage_stub):
    await _user(session, _UID)
    await _user(session, _OTHER_UID)
    pid, _jid = await _project_with_job(session, uid=_OTHER_UID)

    resp = await client.get(f"/v1/projects/{pid}/source", headers=_auth())
    assert resp.status_code == 404
