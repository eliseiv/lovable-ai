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

## Alternatives

- **Только graceful-fail агента (B) без расширения reconciler (A).** Отвергнут: при смерти воркера до записи перехода джоба всё равно зависнет — нужна reconciler-страховка по TTL.
- **Только расширение reconciler (A) без graceful-fail (B).** Отвергнут: слот держался бы до `STUCK_THRESHOLD_S` (15 мин) на каждой LLM-недоступности — деградация UX; graceful-fail освобождает слот сразу.
- **Терминализировать по `updated_at` без новой колонки.** Отвергнут: cost-ledger пишет `updated_at` при каждом `llm_usage` → heartbeat ложно «свежий», stuck-джоба не детектится. Выделенный `last_transition_at` корректен.
- **Переиспользовать `infra_error` для LLM-недоступности.** Отвергнут: смешивает «Claude недоступен» с Docker/S3/БД-сбоями — теряется диагностический сигнал и точность дашборда.
