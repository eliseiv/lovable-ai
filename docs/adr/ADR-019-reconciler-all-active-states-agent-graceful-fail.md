# ADR-019 — Reconciler покрывает ВСЕ активные нетерминальные состояния + graceful-fail агента при недоступности LLM

**Статус:** Accepted · **Дата:** 2026-06-03

## Context

Прод-инцидент (`corelysite.shop`): когда LLM-агент не мог выполниться (`ANTHROPIC_API_KEY` отсутствует/невалиден, либо Claude отвечал `5xx`/`429`/timeout), джоба оставалась в активном нетерминальном состоянии (наблюдалось `INTERVIEWING`) и **не** переходила в `FAILED`. Она продолжала занимать concurrency-слот (`max_concurrent_jobs`, [billing §4](../modules/billing/03-architecture.md#4-entitlements--quota-gate)) → следующий `POST /projects` получал `402 concurrency_limit`. Корневые причины:

1. **Reconciler ([pipeline §E2](../modules/pipeline/03-architecture.md#e2-reconciler-застрявших-активных-состояний-crash-resume--concurrency-leak-guard-adr-019)) покрывал только `{BUILDING, DEPLOYING, FIXING}`** — LLM-фазные активные состояния (`CREATED`, `INTERVIEWING`, `SPECCING`) не подхватывались. Зависшая в них джоба никогда не терминализировалась.
2. **Шаг агента не делал graceful-перехода в `FAILED` при недоступности LLM** — таска либо бесконечно крутила Celery-retry (транзиентные `429`/`5xx`), либо «висела»/терялась без терминализации, а на не-транзиентном `401` (нет/невалиден ключ) поведение было неопределённым.

`AWAITING_CLARIFICATION` к проблеме не относится — у него собственный TTL-sweeper (§E1, 7 дней): это штатная пауза human-in-the-loop, а не зависание.

## Decision

**Два согласованных изменения нормативного контракта resilience (ADR-001 не отменяется — уточняется):**

### A. Reconciler покрывает ВСЕ активные нетерминальные состояния
Скоуп reconciler'а расширяется с `{BUILDING, DEPLOYING, FIXING}` до полного набора non-terminal состояний, удерживающих concurrency-слот:
`CREATED, INTERVIEWING, SPECCING, BUILDING, DEPLOYING, FIXING` (исключён `AWAITING_CLARIFICATION` — отдельный TTL §E1; исключены терминалы `LIVE`/`FAILED`).

Stuck-критерий — по **`generation_jobs.last_transition_at`** (новый heartbeat-столбец, обновляется только при смене `state`) `< now() - STUCK_THRESHOLD_S` (default 900 s), а не по `updated_at` (последний дёргается cost-ledger'ом и ложно сбрасывал бы heartbeat).

Две ветви действия:
- **resumable-состояния `BUILDING`/`DEPLOYING`/`FIXING`** — ре-диспетчеризация таски по текущему `state` (как было, идемпотентно через cleanup-before-run);
- **LLM-фазные `CREATED`/`INTERVIEWING`/`SPECCING`** без живой таски — **fail-stuck**: транзакционный перевод в `FAILED(stuck_timeout)`, освобождение слота. Предохранитель на случай, когда graceful-fail (B) не сработал из-за смерти воркера.

### B. Graceful-fail шага агента при недоступности LLM
**Ни одна агент-таска не имеет «пути в никуда»** — любой исход ведёт к продвижению `state` либо к терминальному `FAILED`:
- **Транзиентные** сбои Claude (`429`/`APIStatusError(5xx)`/timeout/`ConnectionError`) — Celery autoretry (как [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md), распространяется на **все** агент-таски). **Исчерпание `max_retries`** → graceful-переход в **`FAILED(agent_unavailable)`**.
- **Не-транзиентные** сбои (`AuthenticationError`/`401` — нет/невалиден ключ; `403`; `400`) — **НЕ ретраятся**, немедленный graceful `FAILED(agent_unavailable)` без сжигания `max_retries`.

Новые reason-коды: **`agent_unavailable`** (LLM недоступен) и **`stuck_timeout`** (reconciler-страховка). `infra_error` сужается до **не-LLM** транзиентных сбоев (Docker/S3/БД). Классификация исключений — та же единственная точка, что §D (`app/workers/retry_policy.py`).

## Consequences

**Плюсы:**
- Concurrency-leak зависших джоб устранён: слот освобождается либо graceful-fail'ом агента (B, основной путь), либо reconciler-страховкой (A, ветвь fail-stuck).
- Чёткий пользовательский сигнал: `FAILED(agent_unavailable)` отличает «Claude недоступен» от прочих инфра-сбоев и build-fail.
- `last_transition_at` даёт корректный heartbeat прогресса, не загрязнённый cost-ledger-апдейтами.

**Минусы / следствия:**
- Новая колонка `generation_jobs.last_transition_at` — миграция + backfill (`= updated_at` существующих строк) + обновление во всех точках смены `state`.
- Расширенный скоуп reconciler'а требует аккуратной защиты от двойной терминализации (`FOR UPDATE SKIP LOCKED` + Redis-lock `dispatch:{job_id}`), чтобы не терминализировать джобу с ещё-бегущей таской.
- Метрики наблюдаемости (`lovable_job_failed_total{reason}`, `lovable_jobs_in_state{state}`) уже параметризованы перечнем reason-кодов/состояний — новые коды попадают в них автоматически ([observability §2.1](../modules/observability/03-architecture.md#21-pipeline--jobs)).

## Fix (2026-06-04) — loop-affinity Redis-клиента в Celery (прод-инцидент)

> Прод-E2E (`corelysite.shop`) выявил, что фикс A/B неполон: джобы всё равно зависали в `INTERVIEWING`, а reconciler флапал. **Это уточнение действующего ADR-019** (тот же concurrency-leak), а не новый ADR — корень иной, чем A/B, но симптом и инвариант («ни одна агент-таска не зависает, слот освобождается») те же.

**Симптом.** `task_interview` и `beat.reconcile_stuck` падали `RuntimeError: Event loop is closed` / `Future attached to a different loop` **внутри `publish_event()`** (`app/pipeline/events.py`), вызываемого из `transition()` после commit. `publish_event` ловил только `(RedisError, OSError)` → `RuntimeError` пробрасывался и убивал таску **до** вызова Claude → джоба зависала в `INTERVIEWING` → лочила concurrency-слот (тот самый leak, что закрывает A/B). Тот же баг делал safety-net reconciler флапающим.

**Корневая причина.** [observability §7](../modules/observability/03-architecture.md#7-async-выполнение-async-кода-из-синхронной-celery-задачи-нормативный-паттерн-loop-affinity-всех-loop-bound-async-ресурсов) (раунд-1, `metrics.refresh`) сделал per-task **только DB-engine**; глобальный async-Redis `ConnectionPool`-синглтон (`app/observability/redis_pool.py`, [ADR-016](ADR-016-scale-topology-redis-pool.md)/[TD-007](../100-known-tech-debt.md#td-007)) остался привязан к первому/закрытому event loop. На втором таске соединение из пула используется из чужого `asyncio.run`-loop → `RuntimeError`.

**Решение (два слоя, согласовано с [ADR-016](ADR-016-scale-topology-redis-pool.md)):**

1. **Корневой фикс — обобщение loop-affinity §7 на ВСЕ loop-bound async-ресурсы.** НИ ОДИН глобальный async-ресурс, привязанный к loop (DB-engine **И** async-Redis клиент/`ConnectionPool`), не переиспользуется между `asyncio.run`-loop'ами Celery-задач. **Нормативный паттерн Redis в Celery:** клиент/пул, используемый из тела таски (`publish_event` и любые async-Redis вызовы), создаётся **per-task внутри loop задачи** (по аналогии с `worker_engine_scope`), с `aclose()`/`disconnect()` в `finally`, биндинг через **`ContextVar`**. Нормативный контракт — [observability §7.0–7.2](../modules/observability/03-architecture.md#70-принцип-обобщён-на-все-loop-bound-async-ресурсы--нормативно).
2. **Согласование с [ADR-016](ADR-016-scale-topology-redis-pool.md) — пути разведены.** Глобальный ASGI-пул FastAPI (`BlockingConnectionPool`, rate-limit/SSE/budget hot-path) **остаётся** для ASGI-процесса (привязан к долгоживущему ASGI-loop). Воркерный путь (Celery `asyncio.run`) — per-task Redis. Это **физически разные объекты** ([observability §7.2](../modules/observability/03-architecture.md#72-разведение-asgi-пути-и-воркерного-пути-нормативно)); [ADR-016](ADR-016-scale-topology-redis-pool.md) ConnectionPool-синглтон не отменяется, лишь сужается до ASGI.
3. **Второй слой (не маскировка корня) — best-effort `publish_event`.** publish в Redis — best-effort нотификация (источник истины — `state`+`job_events`, SSE имеет replay [ADR-012](ADR-012-sse-realtime-transport.md)); сбой publish **не** валит транзакцию/таску перехода state. `publish_event` ловит **`(RedisError, OSError, RuntimeError)`** (добавлен `RuntimeError`), логирует, **не пробрасывает** — чтобы переход state и graceful-fail (B) доходили до терминала даже при сбое нотификации. Нормативный контракт — [pipeline §H](../modules/pipeline/03-architecture.md#h-publish_event--best-effort-нотификация-не-валит-переход-state-adr-019). **Корневой фикс — п.1 (loop-binding Redis); §H — предохранитель, не замена.**

**Следствие.** После фикса reconciler и graceful-fail (B) **надёжно** терминализируют джобу и освобождают слот при пустом/невалидном `ANTHROPIC_API_KEY` (нет флапа из-за `RuntimeError` в publish). Без новой внешней зависимости — `redis.asyncio` уже в стеке ([02-tech-stack.md](../02-tech-stack.md), Redis 7.x; клиент `redis-py` async).

## Alternatives

- **Только graceful-fail агента (B) без расширения reconciler (A).** Отвергнут: при смерти воркера до записи перехода джоба всё равно зависнет — нужна reconciler-страховка по TTL.
- **Только расширение reconciler (A) без graceful-fail (B).** Отвергнут: слот держался бы до `STUCK_THRESHOLD_S` (15 мин) на каждой LLM-недоступности — деградация UX; graceful-fail освобождает слот сразу.
- **Терминализировать по `updated_at` без новой колонки.** Отвергнут: cost-ledger пишет `updated_at` при каждом `llm_usage` → heartbeat ложно «свежий», stuck-джоба не детектится. Выделенный `last_transition_at` корректен.
- **Переиспользовать `infra_error` для LLM-недоступности.** Отвергнут: смешивает «Claude недоступен» с Docker/S3/БД-сбоями — теряется диагностический сигнал и точность дашборда.
- **Отдельный ADR для Redis loop-affinity (вместо §Fix здесь).** Отвергнут: loop-affinity Redis — **тот же** прод-инцидент concurrency-leak (джоба зависает в `INTERVIEWING`, слот не освобождается), что и A/B; инвариант и симптом совпадают, отличается лишь корневая причина. По канону «уточнение действующего ADR ≠ новый ADR» (ср. [ADR-017 §Fix](ADR-017-path-based-site-routing.md)) фикс зафиксирован как §Fix ADR-019 + расширение нормативных контрактов ([observability §7](../modules/observability/03-architecture.md#7-async-выполнение-async-кода-из-синхронной-celery-задачи-нормативный-паттерн-loop-affinity-всех-loop-bound-async-ресурсов), [pipeline §H](../modules/pipeline/03-architecture.md#h-publish_event--best-effort-нотификация-не-валит-переход-state-adr-019)), согласован с [ADR-016](ADR-016-scale-topology-redis-pool.md).
- **Только best-effort `publish_event` (§H) без loop-fix (п.1).** Отвергнут как маскировка корня: поглощение `RuntimeError` скрыло бы, что Redis-соединения протекают между loop'ами (деградация/утечка под нагрузкой), а реальная SSE-нотификация так и не уходила бы. Loop-fix — обязательный корень; §H — только предохранитель.
