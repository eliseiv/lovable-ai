# ADR-006 — Разграничение Celery-retry (инфра-сбой) vs доменный FIXING (build-fail)

**Статус:** Accepted
**Дата:** 2026-06-02
**Связано:** Sprint 2 (resilience), [modules/pipeline/03-architecture.md](../modules/pipeline/03-architecture.md), [ADR-001](ADR-001-state-machine-dispatcher.md), [ADR-003](ADR-003-celery-vs-rq.md)

## Context

В Sprint 2 у джобы есть **два принципиально разных вида неудачи**, и их нельзя смешивать:

1. **Инфраструктурный (транзиентный) сбой таски** — Docker daemon недоступен, сетевой timeout к S3/Anthropic/Docker, `429` rate-limit Claude, временная ошибка БД/Redis. Причина — окружение, а не код сайта. Правильная реакция — **повторить ту же таску** (тот же шаг пайплайна), желательно с backoff. Это **не** означает, что сгенерированный сайт плох.
2. **Доменный build-fail** — `npm ci`/`vite build` вернул ненулевой код, дерево не собралось, health-check не отдал 200, output агента не прошёл валидацию. Причина — **сам сгенерированный код**. Повтор той же таски бессмысленен (тот же код → тот же фейл). Правильная реакция — отдать дело **Agent 4** через состояние `FIXING`, инкрементируя доменный `retry_count` (на применённом патче, см. pipeline §C(a)) и проверяя гарды.

Если перепутать: ретраить build-fail Celery-ретраями — сожжём `max_retries` на детерминированно-падающем коде и **не** инкрементируем доменный `retry_count` (гарды fix-loop не сработают, no-progress не посчитается); а гнать инфра-timeout в `FIXING` — заставим Agent 4 «чинить» исправный код из-за того, что упала сеть, и впустую потратим Claude-$ + один из `max_fix_attempts`.

## Decision

Жёстко разделяем два механизма по **типу исключения**, а не по месту возникновения.

### Транзиентные инфра-сбои → Celery autoretry с exponential backoff

- Список **ретраябельных** исключений (транзиентные): ошибки Docker daemon/CLI транспорта, сетевые таймауты/`ConnectionError` к S3/Anthropic/Docker, `anthropic.RateLimitError`/`APIStatusError(5xx)`/`429`, временные ошибки БД/Redis-брокера.
- Конфиг таски: `autoretry_for=(<transient exc set>)`, `retry_backoff=True` (exponential), `retry_backoff_max`, `retry_jitter=True`, `max_retries` (default 5), `acks_late=True` + `reject_on_worker_lost=True` (crash-resume, согласовано с cleanup-before-run из Sprint 1).
- Исчерпание Celery `max_retries` на **инфра**-сбое → джоба → `FAILED(infra_error)` (не `build_unrecoverable`): это не вина кода сайта.

### Доменный build-fail → НЕ Celery-retry, а состояние FIXING

- Доменные исключения (build/health/валидация) **исключены** из `autoretry_for`. Таска ловит их сама, переводит джобу `DEPLOYING → FIXING` (через deploy-teardown + запись `failure_log_ref`), проверяет 4 гарда и при валидном патче Agent 4 идёт `FIXING → BUILDING` (пересборка) с инкрементом `retry_count`. Это доменный цикл, а не Celery-ретрай. Правило инкремента `retry_count` (ровно на применённом патче `FIXING → BUILDING`, не на входе в `FIXING`) — единственный нормативный источник [pipeline §C(a)](../modules/pipeline/03-architecture.md#c-четыре-гарда-от-бесконечного-цикла-и-runaway-затрат).
- Доменный fix-loop **не использует** `task.retry()` — он реализован как переход state-machine + постановка `task_fix` диспетчером (ADR-001). Так fix-итерации видимы в `job_events`/метриках (`fix-loop depth`) и подчинены гардам.

### Инвариант

> Один и тот же фейл никогда не учитывается **обоими** механизмами. `task.retry()` — только для транзиентных инфра-исключений; build/health/validation-fail — только через `FIXING`. Классификация исключения (transient vs domain) — единственная точка решения (`app/workers/retry_policy.py`).

## Consequences

- **Плюс:** гарды fix-loop считают только реальные доменные попытки; Celery-ретраи не загрязняют `retry_count`/no-progress.
- **Плюс:** транзиентный blip (упал S3 на секунду) самовосстанавливается без траты fix-budget и без вовлечения Claude.
- **Плюс:** два разных терминальных reason-кода (`infra_error` vs `build_unrecoverable`) — пользователь/оператор различает «упала инфра» и «код неисправим».
- **Минус / граница:** требуется точная классификация исключений. Ошибочная классификация доменного фейла как транзиентного → бесполезные Celery-ретраи (затем всё равно `FAILED`); обратная — лишний виток Agent 4. Список исключений — единый источник в `app/workers/retry_policy.py`, покрыт unit-тестами (ADR ссылается, [06-testing-strategy.md](../06-testing-strategy.md)).

## Alternatives

- **Всё через Celery-retry** (включая build-fail) — отвергнуто: не вовлекает Agent 4, гарды fix-loop не применяются, no-progress не считается, `acks_late`-повтор детерминированно падающего кода жжёт `max_retries` впустую.
- **Всё через FIXING** (включая инфра-timeout) — отвергнуто: Agent 4 «чинит» исправный код из-за сетевого blip, тратит Claude-$ и один из `max_fix_attempts`.
- **Единый счётчик попыток на оба вида** — отвергнуто: смешивает семантику, ломает диагностику и метрики.
