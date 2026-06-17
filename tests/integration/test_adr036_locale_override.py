"""Integration: применение явного locale-override в _interview (ADR-036 §6/§7/§9).

Реальный Postgres (session_scope, autonomous_db). Внешние границы (Agent 1 LLM-вызов,
S3-storage, dispatch/publish) — моки. Покрывает task-уровень `_interview` (точка выбора
языка ДО Agent 1) + сервис create_project_with_job (запись requested_locale, идемпотентность).

Чек-лист ТЗ (строго по ADR-036, единый источник правила приоритета — pipeline §Язык п.2):
  2. Регрессия-инвариант §9: requested_locale=NULL → _interview вызывает detect_language(prompt)
     байт-в-байт как прежде (content_language == detect_language(prompt).bcp47), без
     language_from_bcp47.
  3. Приоритет §6: requested_locale='ru' при английском boilerplate-prompt → content_language='ru'
     ЧЕРЕЗ language_from_bcp47, БЕЗ вызова detect_language.
  4. Idempotency §7: replay того же Idempotency-Key с другим locale НЕ перезаписывает
     project.requested_locale (created=False, возвращается ДО создания проекта).

dispatcher/normalize_locale/edits-scope покрыты в соседних файлах (unit normalize, contract edits).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import (
    CreditGrant,
    GenerationJob,
    JobEvent,
    LlmUsage,
    Project,
    Question,
    UsageCounter,
    User,
)
from app.db.session import session_scope
from app.pipeline.agents.agent1 import Agent1Result, ParsedQuestion
from app.pipeline.agents.claude_client import AgentCall
from app.pipeline.language import detect_language
from app.workers import tasks as worker_tasks

pytestmark = pytest.mark.asyncio

UID = "u_locale036000000001a"

# Английский технический boilerplate, перевешивающий короткий русский запрос латиницей —
# ровно прод-баг ADR-036 Context: script-детект отдал бы `en` при русском намерении.
_BOILERPLATE_EN_PROMPT = (
    "Technical context: use Material UI design system. [#DESIGN_SYSTEM#] "
    "Use modern responsive layout with clean typography and accessible components. "
    "Provide a production-ready Vite project. "
    "Сделай сайт для кофейни"  # короткий русский пользовательский запрос в конце
)


def _call() -> AgentCall:
    return AgentCall(
        text="raw",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=5,
        cost_usd=Decimal("0.0100"),
    )


async def _purge() -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        if job_ids:
            # FK-safe: дети generation_jobs (llm_usage от cost-ledger after_call хука,
            # questions, job_events) — ДО удаления джоб.
            await s.execute(delete(LlmUsage).where(LlmUsage.job_id.in_(job_ids)))
            await s.execute(delete(Question).where(Question.job_id.in_(job_ids)))
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        pids = (await s.execute(select(Project.id).where(Project.user_id == UID))).scalars().all()
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        # Billing-дети users (usage_counters/credit_grants от count_generation_start) — до users.
        await s.execute(delete(UsageCounter).where(UsageCounter.user_id == UID))
        await s.execute(delete(CreditGrant).where(CreditGrant.user_id == UID))
        from app.db.models import Subscription

        await s.execute(delete(Subscription).where(Subscription.user_id == UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest_asyncio.fixture
async def seeded(autonomous_db):  # noqa: ANN001, ANN201
    await _purge()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("locale036-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        await s.commit()
    yield
    await _purge()


async def _seed_created_job(*, prompt: str, requested_locale: str | None) -> str:
    """Project (с requested_locale) + CREATED generation-джоба. Возвращает job_id."""
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(
            Project(
                id=pid,
                user_id=UID,
                prompt=prompt,
                title=None,
                requested_locale=requested_locale,
            )
        )
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=JobState.CREATED,
                kind="generation",
                idempotency_key=jid,
            )
        )
        await s.commit()
    return jid


def _install_language_spies(monkeypatch):  # noqa: ANN001, ANN202
    """Оборачивает detect_language / language_from_bcp47 в namespace worker_tasks, считая вызовы.

    Возвращает dict со списками аргументов вызовов — для ассертов «detect_language НЕ вызван»
    (приоритет locale) или «вызван байт-в-байт» (регрессия NULL).
    """
    calls: dict[str, list] = {"detect": [], "from_bcp47": []}

    real_detect = worker_tasks.detect_language
    real_from = worker_tasks.language_from_bcp47

    def _spy_detect(prompt):  # noqa: ANN001, ANN202
        calls["detect"].append(prompt)
        return real_detect(prompt)

    def _spy_from(bcp47):  # noqa: ANN001, ANN202
        calls["from_bcp47"].append(bcp47)
        return real_from(bcp47)

    monkeypatch.setattr(worker_tasks, "detect_language", _spy_detect)
    monkeypatch.setattr(worker_tasks, "language_from_bcp47", _spy_from)
    return calls


def _install_fake_agent1(monkeypatch, captured: dict) -> None:  # noqa: ANN001
    """Мокает run_agent1: ассертит сигнатуру хуков, фиксирует переданный язык, возвращает
    детерминированные вопросы (LLM не вызывается)."""

    async def _fake_agent1(  # noqa: ANN202
        settings, prompt, language, *, before_call, after_call, on_attempt_failure, images=None
    ):  # noqa: ANN001
        captured["language_to_agent1"] = language.bcp47
        await before_call()
        call = _call()
        await after_call(call)
        return Agent1Result(
            questions=[ParsedQuestion(position=1, text="Q1?", kind="free_text", options=None)],
            call=call,
        )

    monkeypatch.setattr(worker_tasks, "run_agent1", _fake_agent1)


# --------------------------------------------------------------------------- #
# 2. Регрессия-инвариант (§9): requested_locale=NULL → detect_language(prompt), байт-в-байт.
# --------------------------------------------------------------------------- #


async def test_null_locale_uses_detect_language_byte_for_byte(seeded, monkeypatch):
    """requested_locale=NULL → _interview вызывает detect_language(prompt); content_language ==
    detect_language(prompt).bcp47 (байт-в-байт как до фичи), language_from_bcp47 НЕ вызван.

    Прод-инвариант обратной совместимости ADR-036 §9: клиенты без locale работают как прежде.
    """
    prompt = "Build a personal blog about hiking and travel photography"
    expected = detect_language(prompt).bcp47  # эталон «как без фичи»
    assert expected == "en"

    calls = _install_language_spies(monkeypatch)
    captured: dict = {}
    _install_fake_agent1(monkeypatch, captured)

    jid = await _seed_created_job(prompt=prompt, requested_locale=None)
    await worker_tasks._interview(jid)

    # detect_language вызван ровно на этом промпте; language_from_bcp47 НЕ вызван (NULL-ветка).
    assert calls["detect"] == [prompt], "NULL-locale обязан звать detect_language(project.prompt)"
    assert calls["from_bcp47"] == [], "NULL-locale НЕ должен звать language_from_bcp47"

    # content_language зафиксирован = результат detect_language (байт-в-байт прежнее поведение).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.content_language == expected
        assert job.state == JobState.AWAITING_CLARIFICATION
    # Тот же язык доехал до Agent 1 (серверная директива).
    assert captured["language_to_agent1"] == expected


async def test_null_locale_cyrillic_prompt_detects_ru(seeded, monkeypatch):
    """Контр-проверка регрессии: NULL + чисто русский промпт → detect_language → ru (как прежде)."""
    prompt = "Создай сайт-портфолио фотографа с галереей и контактами"
    calls = _install_language_spies(monkeypatch)
    captured: dict = {}
    _install_fake_agent1(monkeypatch, captured)

    jid = await _seed_created_job(prompt=prompt, requested_locale=None)
    await worker_tasks._interview(jid)

    assert calls["detect"] == [prompt]
    assert calls["from_bcp47"] == []
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.content_language == "ru"


# --------------------------------------------------------------------------- #
# 3. Приоритет (§6): requested_locale='ru' при английском boilerplate → ru БЕЗ detect_language.
# --------------------------------------------------------------------------- #


async def test_explicit_ru_locale_overrides_english_boilerplate_detection(seeded, monkeypatch):
    """requested_locale='ru' + английский boilerplate-prompt (латиница перевесила бы детект на en)
    → content_language='ru' ЧЕРЕЗ language_from_bcp47, detect_language НЕ вызван (приоритет §6).

    Ровно прод-фикс ADR-036: явный клиентский locale побеждает загрязнённый script-детект.
    """
    # Санити: на этом промпте авто-детект отдал бы en (boilerplate перевешивает) — locale спасает.
    assert detect_language(_BOILERPLATE_EN_PROMPT).bcp47 == "en"

    calls = _install_language_spies(monkeypatch)
    captured: dict = {}
    _install_fake_agent1(monkeypatch, captured)

    jid = await _seed_created_job(prompt=_BOILERPLATE_EN_PROMPT, requested_locale="ru")
    await worker_tasks._interview(jid)

    # Приоритет: language_from_bcp47('ru'), detect_language вообще НЕ вызывается.
    assert calls["from_bcp47"] == ["ru"], "explicit locale обязан звать language_from_bcp47"
    assert calls["detect"] == [], "explicit locale НЕ должен звать detect_language (приоритет §6)"

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.content_language == "ru", "явный ru переопределил en-детект boilerplate"
        assert job.state == JobState.AWAITING_CLARIFICATION
    # ru доехал до Agent 1 директивой (вопросы на русском).
    assert captured["language_to_agent1"] == "ru"


async def test_explicit_en_locale_overrides_cyrillic_detection(seeded, monkeypatch):
    """Симметрия: requested_locale='en' + кириллический промпт → en через language_from_bcp47,
    detect_language НЕ вызван (явный locale побеждает в обе стороны, §6)."""
    prompt = "Корпоративный сайт для юридической фирмы"
    assert detect_language(prompt).bcp47 == "ru"  # авто-детект отдал бы ru

    calls = _install_language_spies(monkeypatch)
    captured: dict = {}
    _install_fake_agent1(monkeypatch, captured)

    jid = await _seed_created_job(prompt=prompt, requested_locale="en")
    await worker_tasks._interview(jid)

    assert calls["from_bcp47"] == ["en"]
    assert calls["detect"] == []
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.content_language == "en"


# --------------------------------------------------------------------------- #
# 4. Idempotency (§7): replay того же ключа с другим locale НЕ перезаписывает requested_locale.
# --------------------------------------------------------------------------- #


async def test_idempotent_replay_different_locale_does_not_overwrite(seeded):
    """replay того же (user, Idempotency-Key) с ДРУГИМ locale → created=False, та же джоба;
    project.requested_locale остаётся значением ПЕРВОГО запроса (ADR-036 §7).

    requested_locale пишется ТОЛЬКО на created=True; replay возвращается ДО создания проекта
    (idempotency-резолв в create_project_with_job), второй locale игнорируется — не перезапись.
    """
    # Мок enqueue (Celery не нужен) — патчим точку вызова в сервисе.
    import app.pipeline.dispatcher as disp
    import app.services.project_service as project_mod
    from app.services.project_service import create_project_with_job

    orig = project_mod.dispatch_for_state
    project_mod.dispatch_for_state = lambda *a, **k: None  # noqa: E731
    disp_orig = disp.dispatch_for_state
    disp.dispatch_for_state = lambda *a, **k: None  # noqa: E731
    try:
        # Первый запрос: locale=ru.
        async with session_scope() as s:
            r1 = await create_project_with_job(
                s,
                user_id=UID,
                prompt="p",
                title=None,
                idempotency_key="locale-replay-key",
                requested_locale="ru",
            )
        assert r1.created is True

        # Replay того же ключа с ДРУГИМ locale=en → идемпотентный возврат, created=False.
        async with session_scope() as s:
            r2 = await create_project_with_job(
                s,
                user_id=UID,
                prompt="p",
                title=None,
                idempotency_key="locale-replay-key",
                requested_locale="en",
            )
        assert r2.created is False, "replay того же ключа → created=False (idempotency §7)"
        assert r2.job_id == r1.job_id
        assert r2.project_id == r1.project_id

        # requested_locale проекта остался от ПЕРВОГО запроса (ru), НЕ перезаписан на en.
        async with session_scope() as s:
            project = await s.get(Project, r1.project_id)
            assert project.requested_locale == "ru", (
                "replay с другим locale НЕ должен перезаписывать requested_locale (§7)"
            )
            # Ровно одна джоба по ключу — дубль не создан.
            from sqlalchemy import func

            count = await s.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.idempotency_key == "locale-replay-key")
            )
            assert count == 1
    finally:
        project_mod.dispatch_for_state = orig
        disp.dispatch_for_state = disp_orig


async def _seed_pro_subscription() -> None:
    """Pro-подписка (max_projects=null) — изолирует тест прокидки locale от free project_limit."""
    from datetime import UTC, datetime

    from app.core.ids import new_subscription_id
    from app.db.models import Subscription

    async with session_scope() as s:
        s.add(
            Subscription(
                id=new_subscription_id(),
                user_id=UID,
                access_level="pro",
                status="active",
                will_renew=True,
                raw={},
                synced_at=datetime.now(UTC),
            )
        )
        await s.commit()


async def test_service_persists_normalized_locale_on_create(seeded):
    """create_project_with_job(requested_locale='ru') на created=True пишет project.requested_locale
    = 'ru' (прокидка роутер→сервис→Project, ADR-036 §5). NULL-дефолт остаётся NULL.

    Pro-подписка (max_projects=null) изолирует от free project_limit — тест проверяет прокидку
    locale, а не quota-gate (он покрыт отдельно в test_projects_idempotency).
    """
    import app.pipeline.dispatcher as disp
    import app.services.project_service as project_mod
    from app.services.project_service import create_project_with_job

    await _seed_pro_subscription()

    orig = project_mod.dispatch_for_state
    project_mod.dispatch_for_state = lambda *a, **k: None  # noqa: E731
    disp_orig = disp.dispatch_for_state
    disp.dispatch_for_state = lambda *a, **k: None  # noqa: E731
    try:
        async with session_scope() as s:
            r_ru = await create_project_with_job(
                s,
                user_id=UID,
                prompt="p",
                title=None,
                idempotency_key="loc-ru",
                requested_locale="ru",
            )
        async with session_scope() as s:
            r_null = await create_project_with_job(
                s,
                user_id=UID,
                prompt="p",
                title=None,
                idempotency_key="loc-null",
                requested_locale=None,
            )
        async with session_scope() as s:
            assert (await s.get(Project, r_ru.project_id)).requested_locale == "ru"
            assert (await s.get(Project, r_null.project_id)).requested_locale is None
    finally:
        project_mod.dispatch_for_state = orig
        disp.dispatch_for_state = disp_orig
