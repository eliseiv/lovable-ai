"""Integration: POST /projects идемпотентность + гонка (docs/06-testing-strategy.md).

Реальный Postgres. dispatch_for_state/publish мокаются (Celery/Redis enqueue не нужны).
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.db.enums import JobState
from app.db.models import GenerationJob, Project
from app.db.session import session_scope
from app.services.project_service import create_project_with_job

pytestmark = pytest.mark.asyncio


async def test_create_project_returns_202_with_ids(
    client, auth_headers, seeded_user, no_side_effects
):
    # ADR-034 §D11: POST /projects — multipart (prompt/title как Form). Текстовый путь без images.
    resp = await client.post(
        "/v1/projects",
        data={"prompt": "A landing page for my cafe", "title": "Cafe"},
        headers={**auth_headers, "Idempotency-Key": "key-1"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["project_id"].startswith("p_")
    assert body["job_id"].startswith("j_")
    # enqueue стартовой задачи (CREATED).
    assert no_side_effects["dispatched"], "стартовая задача не поставлена"


async def test_idempotent_repeat_same_key_returns_same_job(
    client, auth_headers, seeded_user, no_side_effects
):
    headers = {**auth_headers, "Idempotency-Key": "key-dup"}
    r1 = await client.post("/v1/projects", data={"prompt": "p"}, headers=headers)
    r2 = await client.post("/v1/projects", data={"prompt": "p"}, headers=headers)
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert r1.json()["project_id"] == r2.json()["project_id"]


async def test_http_replay_under_exhausted_cap_returns_202_same_job_not_402(
    client, auth_headers, session, seeded_user, no_side_effects
):
    """HTTP-уровень: free-юзер active=1>=cap=1 и projects=1>=max=1 повторяет тот же
    Idempotency-Key через POST /v1/projects → 202 та же джоба (created обходит gate), НЕ 402.

    Прямой контракт follow_up_for_qa: replay free-юзера под исчерпанной квотой не ловит 402,
    т.к. quota-gate перенесён ПОСЛЕ idempotency-резолва в сервисе (docs/billing/03 §4).
    """
    from app.core.ids import new_job_id, new_project_id

    # Победитель: активная (BUILDING) джоба seeded_user с тем же ключом — исчерпывает
    # free cap=1 и создаёт 1 проект (projects=1 >= free max=1).
    pid = new_project_id()
    winner_jid = new_job_id()
    session.add(Project(id=pid, user_id=seeded_user.id, prompt="winner", title=None))
    session.add(
        GenerationJob(
            id=winner_jid,
            project_id=pid,
            user_id=seeded_user.id,
            state=JobState.BUILDING,
            kind="generation",
            idempotency_key="http-replay-key",
        )
    )
    await session.flush()

    resp = await client.post(
        "/v1/projects",
        data={"prompt": "replay"},
        headers={**auth_headers, "Idempotency-Key": "http-replay-key"},
    )
    assert resp.status_code == 202, f"replay под исчерпанным cap → 202, не 402: {resp.text}"
    body = resp.json()
    assert body["job_id"] == winner_jid, "replay вернул существующую джобу-победителя"
    # Ровно одна джоба по ключу — дубль не создан.
    count = await session.scalar(
        select(func.count())
        .select_from(GenerationJob)
        .where(GenerationJob.idempotency_key == "http-replay-key")
    )
    assert count == 1


async def test_http_real_new_request_under_exhausted_cap_returns_402(
    client, auth_headers, session, seeded_user, no_side_effects
):
    """HTTP-уровень: реальный новый запрос (НОВЫЙ ключ) при исчерпанном free cap/projects →
    402 application/problem+json (reason project_limit). Контр-пара к replay-тесту выше."""
    from app.core.ids import new_job_id, new_project_id

    pid = new_project_id()
    session.add(Project(id=pid, user_id=seeded_user.id, prompt="occupant", title=None))
    session.add(
        GenerationJob(
            id=new_job_id(),
            project_id=pid,
            user_id=seeded_user.id,
            state=JobState.BUILDING,
            kind="generation",
            idempotency_key="occupant-http-key",
        )
    )
    await session.flush()

    resp = await client.post(
        "/v1/projects",
        data={"prompt": "fresh"},
        headers={**auth_headers, "Idempotency-Key": "fresh-http-key"},
    )
    assert resp.status_code == 402, f"новый ключ под исчерпанной квотой → 402: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["reason"] == "project_limit"


async def test_missing_idempotency_key_returns_422(client, auth_headers, seeded_user):
    # ADR-034 §D11: multipart, prompt присутствует, Idempotency-Key отсутствует → 422 (наш
    # явный unprocessable, не FastAPI-валидация формы).
    resp = await client.post("/v1/projects", data={"prompt": "p"}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_service_repeat_returns_created_false(session, seeded_user, no_side_effects):
    r1 = await create_project_with_job(
        session, user_id=seeded_user.id, prompt="p", title=None, idempotency_key="svc-1"
    )
    r2 = await create_project_with_job(
        session, user_id=seeded_user.id, prompt="p", title=None, idempotency_key="svc-1"
    )
    assert r1.created is True
    assert r2.created is False
    assert r1.job_id == r2.job_id
    # Ровно одна джоба в БД с этим ключом.
    count = await session.scalar(
        select(func.count())
        .select_from(GenerationJob)
        .where(GenerationJob.idempotency_key == "svc-1")
    )
    assert count == 1


# --- Конкурентная гонка двух INSERT с одним (user_id, idempotency_key) ---
# Нужны ОТДЕЛЬНЫЕ соединения/транзакции → используем session_scope к реальной БД
# и сами чистим за собой (вне общей rollback-сессии).


async def _purge_user_data(uid: str) -> None:
    """Идемпотентная очистка всех данных пользователя (порядок FK-safe).

    Чистит зависимые строки (job_events, generation_jobs, projects) + billing-данные S3.5
    (subscriptions, usage_counters), у которых FK на users — иначе DELETE users падает
    ForeignKeyViolation (fk_subscriptions_user).
    """
    from sqlalchemy import delete

    from app.db.models import JobEvent, Subscription, UsageCounter
    from app.db.models import User as U

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
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        # Снимаем ссылку projects.current_revision_id перед удалением (на всякий случай).
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in set(pids):
            await s.execute(delete(Project).where(Project.id == pid))
        # Billing FK-зависимости S3.5 (subscriptions/usage_counters → users).
        await s.execute(delete(Subscription).where(Subscription.user_id == uid))
        await s.execute(delete(UsageCounter).where(UsageCounter.user_id == uid))
        await s.execute(delete(U).where(U.id == uid))
        await s.commit()


@pytest_asyncio.fixture
async def concurrent_user(autonomous_db):  # noqa: ANN001, ANN201
    """Создаёт пользователя в автономной транзакции; удаляет в teardown."""
    from decimal import Decimal

    from app.core.security import hash_api_key
    from app.db.models import User

    uid = "u_concurrency000000000000"
    await _purge_user_data(uid)
    async with session_scope() as s:
        s.add(
            User(
                id=uid,
                api_key_hash=hash_api_key("conc-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        await s.commit()
    yield uid
    await _purge_user_data(uid)


async def test_concurrent_race_one_winner_no_500(autonomous_db, concurrent_user, monkeypatch):
    """Две параллельные джобы с одним ключом → одна created=True, одна created=False, без 500."""
    import app.pipeline.dispatcher as disp

    monkeypatch.setattr(disp, "dispatch_for_state", lambda *a, **k: None)

    async def _one():  # noqa: ANN202
        async with session_scope() as s:
            return await create_project_with_job(
                s, user_id=concurrent_user, prompt="race", title=None, idempotency_key="race-key"
            )

    results = await asyncio.gather(_one(), _one(), return_exceptions=True)
    # Ни одного исключения (без 500).
    assert all(not isinstance(r, Exception) for r in results), results
    created_flags = sorted(r.created for r in results)
    assert created_flags == [False, True], "ровно один создатель, второй идемпотентный"
    # Обе вернули одну и ту же джобу.
    assert results[0].job_id == results[1].job_id
    # В БД ровно одна джоба.
    async with session_scope() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.idempotency_key == "race-key")
        )
    assert count == 1


# --- Idempotency-aware quota-gate: cap×idempotency (project_service §quota-gate, S3.5) ---
# Контракт S3.5 (docs/modules/billing/03-architecture.md §4, project_service §44-68):
# quota-gate перенесён из FastAPI-dependency ВНУТРЬ create_project_with_job ПОСЛЕ
# idempotency-резолва (_find_existing_job). Следствия, фиксируемые ниже детерминированно:
#   1. idempotent replay (совпадение по (user_id, idempotency_key)) возвращается ДО gate →
#      created=False, БЕЗ enforce_quota_gate → НЕ 402, даже под исчерпанным cap/projects;
#   2. реальный новый запрос (нет idempotency-совпадения) под исчерпанным cap → gate бросает
#      ProblemException(status=402, reason='concurrency_limit') (НЕ удалённый
#      ConcurrencyCapExceeded, НЕ 429). cap каноникализирован в 402 (docs §4 «Каноникализация»).
# Удалённый класс ConcurrencyCapExceeded заменён на ProblemException из quota_gate.


async def _make_active_job_for(session, user_id: str, *, idem_key: str | None) -> str:  # noqa: ANN001
    """Создаёт активную (BUILDING) джобу пользователя с заданным idempotency-key.

    Активная джоба исчерпывает free-cap (=1) → последующий create_project_with_job
    упрётся в quota-gate concurrency-проверку. idem_key=None — «чужой» конкурент без
    idempotency-совпадения; idem_key=<key> — «победитель» той же idempotency-гонки.
    """
    from app.core.ids import new_job_id, new_project_id

    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="winner", title=None))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=user_id,
            state=JobState.BUILDING,
            kind="generation",
            idempotency_key=idem_key,
        )
    )
    await session.flush()
    return jid


async def _make_pro_subscription(session, user_id: str) -> None:  # noqa: ANN001
    """Pro-подписка (max_projects=null) — изолирует concurrency_limit от project_limit.

    Free упёрся бы в project_limit раньше concurrency (gate проверяет projects перед cap,
    docs §4 п.2 перед п.3). Pro: max_projects=null → project_limit не сработает, cap=3.
    """
    from datetime import UTC, datetime

    from app.core.ids import new_subscription_id
    from app.db.models import Subscription

    session.add(
        Subscription(
            id=new_subscription_id(),
            user_id=user_id,
            access_level="pro",
            status="active",
            will_renew=True,
            raw={},
            synced_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def test_idempotent_replay_under_exhausted_cap_returns_created_false_not_402(
    autonomous_db, concurrent_user
):
    """free-юзер active=1>=cap=1 и projects=1>=max=1 повторяет тот же Idempotency-Key →
    существующая джоба (created=False, тот же job_id), НЕ 402.

    quota-gate перенесён ПОСЛЕ idempotency-резолва: replay возвращается до gate, превышение
    cap/projects его не касается (docs §4, project_service §44-62).
    """
    # Победитель: активная (BUILDING) джоба с тем же ключом исчерпывает free cap=1 и
    # одновременно создаёт 1 проект (projects=1 >= free max=1).
    async with session_scope() as s:
        winner_jid = await _make_active_job_for(s, concurrent_user, idem_key="replay-key")
        await s.commit()

    # Повтор с тем же ключом под исчерпанным cap+projects → идемпотентный возврат, НЕ 402.
    async with session_scope() as s:
        result = await create_project_with_job(
            s, user_id=concurrent_user, prompt="replay", title=None, idempotency_key="replay-key"
        )

    assert result.created is False, "idempotent replay под исчерпанным cap → created=False, не 402"
    assert result.job_id == winner_jid, "replay вернул джобу-победителя"
    # Лишняя джоба НЕ создана: в БД по ключу ровно одна (победитель).
    async with session_scope() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.idempotency_key == "replay-key")
        )
    assert count == 1


async def test_real_new_request_under_exhausted_cap_raises_402_concurrency_limit(
    autonomous_db, concurrent_user
):
    """Реальный новый запрос (новый ключ) при превышении cap → ProblemException(402,
    reason='concurrency_limit'). НЕ удалённый ConcurrencyCapExceeded, НЕ 429.

    Активная джоба с ДРУГИМ ключом исчерпывает cap (нет idempotency-совпадения) → gate
    срабатывает. Pro-подписка изолирует concurrency_limit от project_limit (docs §4).
    """
    from app.api.errors import ProblemException

    async with session_scope() as s:
        await _make_pro_subscription(s, concurrent_user)
        # Pro cap=3 → три активные джобы с чужими ключами исчерпывают cap.
        for i in range(3):
            await _make_active_job_for(s, concurrent_user, idem_key=f"other-key-{i}")
        await s.commit()

    with pytest.raises(ProblemException) as exc_info:
        async with session_scope() as s:
            await create_project_with_job(
                s,
                user_id=concurrent_user,
                prompt="real-excess",
                title=None,
                idempotency_key="brand-new-key",
            )

    exc = exc_info.value
    assert exc.status == 402, "cap каноникализирован в 402, не 429"
    assert exc.extra["reason"] == "concurrency_limit"
    assert exc.problem_type == "payment-required"

    # Никакой новой джобы по новому ключу не создано (gate сработал до INSERT).
    async with session_scope() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.idempotency_key == "brand-new-key")
        )
    assert count == 0


async def test_real_new_request_free_project_limit_raises_402(autonomous_db, concurrent_user):
    """free-юзер с projects=1>=max=1, новый ключ → ProblemException(402, reason='project_limit').

    Подтверждает: реальное превышение БЕЗ idempotency-совпадения упирается в gate (free упрётся
    в project_limit раньше concurrency, docs §4 п.2). Контр-пара к idempotent-replay-тесту.
    """
    from app.api.errors import ProblemException

    # Одна активная джоба = 1 проект (free max_projects=1) и cap=1 одновременно.
    async with session_scope() as s:
        await _make_active_job_for(s, concurrent_user, idem_key="occupant-key")
        await s.commit()

    with pytest.raises(ProblemException) as exc_info:
        async with session_scope() as s:
            await create_project_with_job(
                s,
                user_id=concurrent_user,
                prompt="real-excess",
                title=None,
                idempotency_key="fresh-key",
            )

    exc = exc_info.value
    assert exc.status == 402
    assert exc.extra["reason"] == "project_limit"

    async with session_scope() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.idempotency_key == "fresh-key")
        )
    assert count == 0


async def test_usage_counter_not_doubled_on_idempotent_replay(autonomous_db, concurrent_user):
    """usage_counters не двоится на idempotent replay (docs §5, usage.count_generation_start).

    Инкремент привязан к первому старту job_id (job_events-маркер usage_counted). Повтор того
    же job_id (replay) — no-op. Тест моделирует старт победителя + повтор того же job_id.
    """
    from app.billing.usage import count_generation_start, get_usage

    # Победитель + первый старт генерации (инкремент usage).
    async with session_scope() as s:
        winner_jid = await _make_active_job_for(s, concurrent_user, idem_key="usage-key")
        await s.commit()
    async with session_scope() as s:
        job = await s.get(GenerationJob, winner_jid)
        applied_1 = await count_generation_start(s, job)
        await s.commit()
    assert applied_1 is True, "первый старт инкрементит usage"

    # Повтор старта того же job_id (idempotent replay / acks_late) — НЕ инкрементит.
    async with session_scope() as s:
        job = await s.get(GenerationJob, winner_jid)
        applied_2 = await count_generation_start(s, job)
        await s.commit()
    assert applied_2 is False, "повтор того же job_id не инкрементит usage (idempotent)"

    # generations_used ровно 1 (а не 2).
    async with session_scope() as s:
        used = await get_usage(s, concurrent_user)
    assert used == 1, f"usage_counters не должен двоиться на replay, got {used}"
