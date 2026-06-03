"""Integration (Sprint 6, ADR-015): экспозиция /metrics + SSE heartbeat-catchup + concurrency/gc.

Реальный Postgres + Redis (conftest). Внешние границы (Adapty/Docker/S3/APNs) мокаются.

Покрывает:
  - GET /metrics (app ASGI mount) → 200 + prometheus content-type + lovable_* + НЕ под /v1;
  - /metrics не требует Bearer (internal scrape), но не публичен под /v1;
  - SSE heartbeat-catchup (TD-011): терминальное событие записано в job_events, но pub/sub
    wake-сигнал «потерян» (в Redis не публикуем) → heartbeat-таймаут дочитывает job_events +
    event: done без reconnect; метрика sse_heartbeat_catchup_total{tail_replayed};
  - concurrency-block метрика (TD-012): 402 concurrency_limit инкрементит
    concurrency_block_by_kind_total{blocked_kind,holder_kind};
  - gc метрики (TD-010): project_gc_pending gauge / project_gc_duration_seconds.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
from prometheus_client import REGISTRY
from sqlalchemy import delete, select

from app.api import sse
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User
from app.db.session import session_scope
from app.observability import redis_pool
from app.pipeline.events import record_event

pytestmark = pytest.mark.asyncio


# --- /metrics exposition (app ASGI) ---


async def test_metrics_endpoint_returns_prometheus_text(client):  # noqa: ANN001
    """GET /metrics (app mount) → 200, prometheus content-type, содержит lovable_*-метрики.

    Starlette mount канонизирует путь к /metrics/ (307 на bare /metrics); follow_redirects
    повторяет реальное поведение scrape-клиента (Prometheus следует редиректу).
    """
    from prometheus_client import CONTENT_TYPE_LATEST

    resp = await client.get("/metrics", follow_redirects=True)
    assert resp.status_code == 200
    # prometheus text exposition content-type (символ-в-символ с CONTENT_TYPE_LATEST клиента).
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "lovable_jobs_total" in body
    assert "lovable_redis_pool_in_use" in body


async def test_metrics_not_under_v1(client):  # noqa: ANN001
    """/metrics — internal, НЕ под /v1 (под /v1 его нет — 404)."""
    resp = await client.get("/v1/metrics")
    assert resp.status_code == 404


async def test_metrics_no_bearer_required(client):  # noqa: ANN001
    """/metrics доступен без Bearer (internal scrape-target, не доменный /v1-эндпоинт)."""
    resp = await client.get("/metrics", follow_redirects=True)  # без auth_headers
    assert resp.status_code == 200


async def test_metrics_only_get(client):  # noqa: ANN001
    """ASGI /metrics — только GET; POST → 405."""
    resp = await client.post("/metrics/")
    assert resp.status_code == 405


# --- SSE heartbeat-catchup (TD-011) ---

_SSE_UID = "u_hbcatchup0000000001"


async def _purge_sse() -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == _SSE_UID)))
            .scalars()
            .all()
        )
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == _SSE_UID))
        await s.execute(delete(Project).where(Project.user_id == _SSE_UID))
        await s.execute(delete(User).where(User.id == _SSE_UID))
        await s.commit()


async def _make_job(state: JobState) -> str:
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        if await s.get(User, _SSE_UID) is None:
            s.add(
                User(
                    id=_SSE_UID,
                    api_key_hash=hash_api_key("hb-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
        s.add(Project(id=pid, user_id=_SSE_UID, prompt="x", title=None))
        s.add(
            GenerationJob(id=jid, project_id=pid, user_id=_SSE_UID, state=state, kind="generation")
        )
        await s.commit()
    return jid


async def _add_event(jid: str, event_type: str, *, to_state: str | None = None) -> int:
    async with session_scope() as s:
        ev = await record_event(s, jid, event_type, to_state=to_state)
        await s.commit()
        return ev.id


@pytest_asyncio.fixture
async def sse_env(autonomous_db):  # noqa: ANN001, ANN201
    redis_pool.reset_pool_for_tests()
    await _purge_sse()
    yield
    await _purge_sse()


async def test_sse_heartbeat_catchup_reads_terminal_without_pubsub(sse_env):
    """TD-011: терминальное событие в job_events без pub/sub-wake → heartbeat дочитывает + done.

    Сценарий потерянного at-most-once pub/sub: джоба не-терминальна на старте (снимок BUILDING),
    затем переходит в LIVE — событие записано в job_events, НО publish_event в Redis НЕ делается
    (wake-сигнал «потерян»). Heartbeat-таймаут (SSE_HEARTBEAT_S=1 из conftest) должен дочитать
    job_events WHERE id>last_seen и отдать хвост + event: done БЕЗ reconnect.
    """
    jid = await _make_job(JobState.BUILDING)
    await _add_event(jid, "state_changed", to_state="BUILDING")

    before = (
        REGISTRY.get_sample_value(
            "lovable_sse_heartbeat_catchup_total", {"result": "tail_replayed"}
        )
        or 0.0
    )

    frames: list[bytes] = []
    gen = sse.event_stream(jid, last_event_id=None)
    try:
        # 1-й кадр — снимок BUILDING (мгновенно, до heartbeat).
        frames.append(await asyncio.wait_for(gen.__anext__(), timeout=10))
        # Дождаться ПЕРВОГО heartbeat ": ping" — гарантирует, что стрим уже в live-loop
        # ПОСЛЕ catch-up (иначе терминальное событие подхватилось бы catch-up'ом, а не
        # heartbeat-catchup'ом, и мы бы не проверили именно TD-011-ветку).
        ping = await asyncio.wait_for(gen.__anext__(), timeout=10)
        assert ping == b": ping\n\n"
        # Теперь добавляем терминальное событие БЕЗ Redis-publish (потерянный wake-сигнал).
        await _add_event(jid, "state_changed", to_state="LIVE")
        # Стрим по следующему heartbeat-таймауту дочитывает хвост job_events и закрывается done.
        async for frame in gen:
            frames.append(frame)
            if b"event: done" in frame or len(frames) >= 30:
                break
    finally:
        await gen.aclose()

    joined = b"".join(frames).decode()
    assert '"to_state": "LIVE"' in joined, "терминальное событие дочитано без pub/sub-wake"
    assert "event: done" in joined, "стрим закрылся event: done по heartbeat-catchup"

    after = (
        REGISTRY.get_sample_value(
            "lovable_sse_heartbeat_catchup_total", {"result": "tail_replayed"}
        )
        or 0.0
    )
    assert after >= before + 1, "метрика heartbeat-catchup{tail_replayed} инкрементирована"


async def test_sse_heartbeat_noop_when_no_new_events(sse_env):
    """Idle-окно без новых событий → heartbeat ': ping' + метрика catchup{noop}."""
    jid = await _make_job(JobState.BUILDING)
    await _add_event(jid, "state_changed", to_state="BUILDING")

    before = (
        REGISTRY.get_sample_value("lovable_sse_heartbeat_catchup_total", {"result": "noop"}) or 0.0
    )

    gen = sse.event_stream(jid, last_event_id=None)
    saw_ping = False
    try:
        # снимок
        await asyncio.wait_for(gen.__anext__(), timeout=10)
        # ждём heartbeat-кадр (idle, новых событий нет) — должен прийти ": ping".
        for _ in range(3):
            frame = await asyncio.wait_for(gen.__anext__(), timeout=10)
            if frame == b": ping\n\n":
                saw_ping = True
                break
    finally:
        await gen.aclose()

    assert saw_ping, "в idle-окне без событий приходит heartbeat ': ping'"
    after = (
        REGISTRY.get_sample_value("lovable_sse_heartbeat_catchup_total", {"result": "noop"}) or 0.0
    )
    assert after >= before + 1


# --- concurrency-block метрика (TD-012) ---


async def test_concurrency_block_metric_on_402(session):  # noqa: ANN001
    """TD-012: 402 concurrency_limit инкрементит concurrency_block_by_kind_total{blocked,holder}."""
    from app.api.errors import ProblemException
    from app.billing import quota_gate

    uid = "u_concblock0000000001"
    pid = new_project_id()
    session.add(User(id=uid, api_key_hash=hash_api_key("cc-key"), monthly_budget_usd=Decimal("50")))
    await session.flush()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    await session.flush()
    # Активная generation-джоба занимает единственный слот (Free max_concurrent_jobs=1).
    session.add(
        GenerationJob(
            id=new_job_id(),
            project_id=pid,
            user_id=uid,
            state=JobState.BUILDING,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()

    before = (
        REGISTRY.get_sample_value(
            "lovable_concurrency_block_by_kind_total",
            {"blocked_kind": "generation", "holder_kind": "generation"},
        )
        or 0.0
    )

    # Новый старт generation отклоняется (слот занят) → 402 concurrency_limit.
    with pytest.raises(ProblemException) as exc:
        await quota_gate.enforce_quota_gate(
            session, user_id=uid, kind="generation", check_project_limit=False
        )
    assert exc.value.status == 402

    after = (
        REGISTRY.get_sample_value(
            "lovable_concurrency_block_by_kind_total",
            {"blocked_kind": "generation", "holder_kind": "generation"},
        )
        or 0.0
    )
    assert after == before + 1, "concurrency-block метрика инкрементирована с разбивкой по kind"


# --- gc метрики (TD-010) ---


_GC_UID = "u_gcpending0000000001"


async def _purge_gc() -> None:
    async with session_scope() as s:
        await s.execute(delete(Project).where(Project.user_id == _GC_UID))
        await s.execute(delete(User).where(User.id == _GC_UID))
        await s.commit()


async def test_project_gc_pending_gauge_counts_soft_deleted(autonomous_db):  # noqa: ANN001
    """TD-010: project_gc_pending = COUNT(projects WHERE deleted_at IS NOT NULL).

    autonomous_db + session_scope (раздельная транзакция, commit виден refresh-коллектору,
    который открывает свой session_scope). Чистим свои строки до/после (общая БД).
    """
    from datetime import UTC, datetime

    from app.deploy import project_gc

    await _purge_gc()
    try:
        async with session_scope() as s:
            s.add(
                User(
                    id=_GC_UID,
                    api_key_hash=hash_api_key("gc-key"),
                    monthly_budget_usd=Decimal("50"),
                )
            )
            await s.flush()
            # 2 soft-deleted (deleted_at) + 1 живой.
            s.add(
                Project(
                    id=new_project_id(),
                    user_id=_GC_UID,
                    prompt="p",
                    title=None,
                    deleted_at=datetime.now(UTC),
                )
            )
            s.add(
                Project(
                    id=new_project_id(),
                    user_id=_GC_UID,
                    prompt="p",
                    title=None,
                    deleted_at=datetime.now(UTC),
                )
            )
            s.add(Project(id=new_project_id(), user_id=_GC_UID, prompt="p", title=None))
            await s.commit()

        await project_gc._refresh_gc_pending_gauge()
        gauge = REGISTRY.get_sample_value("lovable_project_gc_pending")
        assert gauge is not None
        # >= 2: gauge учитывает наши soft-deleted (общая БД может иметь и чужие — но не меньше 2).
        assert gauge >= 2
    finally:
        await _purge_gc()
