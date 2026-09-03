"""Integration: план сайта, стоимость генерации и выгрузка исходников (ADR-045/046).

Покрытие:
  - `GET /jobs/{jid}/plan` — секции по порядку со статусами; пустой план у задачи без плана;
  - `cost_tokens` в `GET /billing/me` — цена одной генерации в токенах (ADR-049);
  - USD-полей (`cost_usd`, `avg_generation_cost_usd`) в клиентском API больше нет;
  - `GET /projects/{pid}/source` — zip исходников текущей и указанной ревизии, служебный
    `.build.json` в архив не попадает; чужой проект и проект без ревизий → 404.
"""

from __future__ import annotations

import io
import json
import zipfile
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobSection, Project, Revision, User
from app.deploy import workspace
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


async def test_billing_me_exposes_cost_tokens(client, session):
    """Цена генерации в токенах — тарифная величина из настройки, а не расход задачи."""
    await _user(session, _UID)

    resp = await client.get("/v1/billing/me", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["cost_tokens"] == get_settings().generation_cost_tokens


async def test_job_status_has_no_usd_cost(client, session):
    """Себестоимость в USD наружу не отдаётся (ADR-049 отменил `cost_usd`)."""
    await _user(session, _UID)
    _pid, jid = await _project_with_job(session, spend="0.1234")

    body = (await client.get(f"/v1/jobs/{jid}", headers=_auth())).json()
    assert "cost_usd" not in body


async def test_billing_me_has_no_usd_average(client, session):
    """`avg_generation_cost_usd` больше не публикуется (ADR-049)."""
    await _user(session, _UID)

    assert (
        "avg_generation_cost_usd"
        not in (await client.get("/v1/billing/me", headers=_auth())).json()
    )


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


async def test_section_completion_lands_in_job_events(client, session, monkeypatch):
    """Отметка секции пишется в job_events — иначе событие не дошло бы до SSE.

    SSE-кадры формируются ИЗ `job_events` (Redis — лишь wake-сигнал), поэтому запись в БД
    здесь не деталь реализации, а условие доставки прогресса клиенту.
    """
    from sqlalchemy import select

    from app.db.models import JobEvent
    from app.services import plan_service

    await _user(session, _UID)
    _pid, jid = await _project_with_job(session, state=JobState.BUILDING)
    session.add(
        JobSection(job_id=jid, section_id="gallery", title="Галерея", position=0, status="pending")
    )
    await session.commit()

    published: list[tuple[str, str]] = []

    async def fake_publish(job_id, event_type, **kwargs):  # noqa: ANN001, ANN202
        published.append((job_id, event_type))

    @asynccontextmanager
    async def test_session_scope():  # noqa: ANN202
        # Прогресс пишется в СВОЕЙ сессии; в тесте она подменяется на транзакционную
        # сессию стенда, иначе другое соединение не увидит неоткоммиченных данных теста.
        yield session

    monkeypatch.setattr(plan_service, "publish_event", fake_publish)
    monkeypatch.setattr(plan_service, "session_scope", test_session_scope)
    await plan_service._mark_done(jid, "gallery", "Галерея")

    events = (
        (
            await session.execute(
                select(JobEvent).where(
                    JobEvent.job_id == jid, JobEvent.event_type == "section_completed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload == {"section_id": "gallery", "title": "Галерея"}
    assert published == [(jid, "section_completed")]

    row = (
        (
            await session.execute(
                select(JobSection).where(
                    JobSection.job_id == jid, JobSection.section_id == "gallery"
                )
            )
        )
        .scalars()
        .one()
    )
    assert row.status == "done"
    assert row.completed_at is not None


async def test_generation_costs_configured_tokens(client, session, monkeypatch):
    """Списание с бонус-баланса идёт по цене генерации, а не всегда по одному токену."""
    from app.billing import usage
    from app.db.models import UsageCounter

    user = await _user(session, _UID)
    user.bonus_generations_balance = 5
    _pid, jid = await _project_with_job(session, state=JobState.CREATED)
    job = await session.get(GenerationJob, jid)
    assert job is not None
    # Плановая квота исчерпана → списание идёт с токенов.
    session.add(UsageCounter(user_id=_UID, period=usage.current_period(), generations_used=99))
    await session.flush()

    settings = get_settings()
    monkeypatch.setattr(settings, "generation_cost_tokens", 2, raising=False)
    assert await usage.count_generation_start(session, job) is True
    await session.flush()
    await session.refresh(user)
    assert user.bonus_generations_balance == 3


async def test_generation_blocked_when_tokens_below_price(client, session, monkeypatch):
    """Баланса меньше цены генерации → гейт отдаёт 402, а не пропускает задачу бесплатно."""
    from app.api.errors import ProblemException
    from app.billing import usage
    from app.billing.quota_gate import enforce_quota_gate
    from app.db.models import UsageCounter

    user = await _user(session, _UID)
    user.bonus_generations_balance = 1
    session.add(UsageCounter(user_id=_UID, period=usage.current_period(), generations_used=99))
    await session.flush()

    settings = get_settings()
    monkeypatch.setattr(settings, "generation_cost_tokens", 2, raising=False)
    with pytest.raises(ProblemException) as exc:
        await enforce_quota_gate(session, _UID, check_project_limit=False)
    assert exc.value.status == 402
