# ADR-030 — Видимый промежуточный статус `EDITING` для edit-джобы (CREATED → EDITING → BUILDING)

**Статус:** Accepted · **Дата:** 2026-06-12 · **Спринт:** 5 (Realtime & edits, прод-фикс UX)

## Context

Прод-инцидент UX (job `j_p81uvafbmykvan4w7izu8pjx`): edit-джоба (`POST /v1/projects/{pid}/edits`, `kind=edit`) при работе Agent 4 editor (~2–3 мин) **держит `state=CREATED`** — нет ни одного промежуточного видимого перехода. Поток `_edit` ([app/workers/tasks.py](../../app/workers/tasks.py)): вход-guard `state==CREATED` → `record_event(agent_started)` → долгий `run_agent4_editor` (~3 мин) → **только потом** первый `transition(BUILDING)`. Всё это время клиент видит `CREATED` и читает как «зависло».

Для сравнения generation-flow **сразу** уходит `CREATED → INTERVIEWING` (видимый прогресс ещё до длинного Agent 1). У edit-flow такого первого видимого перехода нет — это и есть корень UX-бага.

Два следствия, которые требуют фиксации:

1. **Нет видимого прогресса** на длинном (~3 мин) Agent 4 editor — клиент не отличает «обрабатывается» от «зависло».
2. **`last_transition_at` не двигается** всё время editor'а: джоба в `CREATED` без heartbeat ближе к ложному `stuck_timeout` reconciler'а ([ADR-019 §E2](ADR-019-reconciler-all-active-states-agent-graceful-fail.md), [pipeline §E2](../modules/pipeline/03-architecture.md#e2-reconciler-застрявших-активных-состояний-crash-resume--concurrency-leak-guard-adr-019)) — провисание в одном state дольше `STUCK_THRESHOLD_S`.

**Коллизия диспетчера, которую решение ОБЯЗАНО учесть.** `dispatch_for_state` ([app/pipeline/dispatcher.py](../../app/pipeline/dispatcher.py)) маршрутизирует:
- `CREATED` + `kind=edit` → `task_edit`; `CREATED` + `kind=generation` → `task_interview`;
- `FIXING` → `task_fix` **всегда** (независимо от `kind`).

Поэтому **нельзя** просто перевести edit-джобу в `FIXING` ради видимого статуса: при crash-resume (`acks_late`) reconciler/диспетчер по `state=FIXING` отправит её в `task_fix` (fix-виток с `failure_log`), а **не** в `task_edit` → сломает edit-обработку (Agent 4 в роли fixer без `failure_log`, не editor).

**Тип хранения `state` (подтверждено по коду).** `generation_jobs.state` — **PostgreSQL enum `job_state`**, а не text-колонка: [app/db/models.py](../../app/db/models.py) `Enum(JobState, name="job_state")`; [migrations/versions/20260602_0001_initial_s1.py](../../migrations/versions/20260602_0001_initial_s1.py) `postgresql.ENUM(*_JOB_STATES, name="job_state")`. Значит новое состояние требует миграции `ALTER TYPE job_state ADD VALUE`.

## Decision

### A. Новое состояние `EDITING` (вариант «отдельное состояние»)

Edit-flow получает **видимый промежуточный статус `EDITING`** между `CREATED` и `BUILDING`:

```
CREATED → EDITING → BUILDING → DEPLOYING → LIVE
```

- На старте `_edit` (`kind=edit`, `state==CREATED`), **до** долгого `run_agent4_editor`, джоба переводится `transition(EDITING)` — первый видимый переход появляется за миллисекунды после постановки таски, как `CREATED → INTERVIEWING` у generation. Клиент сразу видит «обрабатывается правка», `last_transition_at` двигается (heartbeat → нет ложного fail-stuck в `CREATED`).
- `EDITING` — **активное нетерминальное LLM-фазное** состояние (Agent 4 editor — LLM-вызов), концептуально аналог `INTERVIEWING`/`SPECCING`.
- После успеха editor'а: `transition(BUILDING)` (как сейчас) → штатный `BUILDING → DEPLOYING → LIVE`. Авто-rollback при провале editor'а (гард/невалидный output/unrecoverable) — без изменений ([ADR-014 §C](ADR-014-edit-limit-revision-rollback.md)), финализирует edit-джобу `FAILED(edit_failed_rolled_back)`.

### B. Диспетчер — новый кейс `EDITING → task_edit`

`dispatch_for_state` получает явную ветвь: `state == EDITING` → `task_edit` (`queue=llm`), **независимо от `kind`** (в `EDITING` оказывается только edit-джоба). Это снимает коллизию `FIXING`: edit-обработка более не делит маршрут с fix-витком — `FIXING → task_fix` остаётся неизменным, `EDITING → task_edit` — отдельный явный маршрут.

> Стартовый `CREATED + kind=edit → task_edit` **сохраняется** (первая постановка таски из `edit_service` идёт по `CREATED`). `EDITING → task_edit` нужен для **crash-resume**: после краша между записью `EDITING` и завершением editor'а reconciler по `state=EDITING` снова ставит `task_edit`.

### C. Guard `_edit` принимает `CREATED` И `EDITING` (crash-resume инвариант)

Вход-guard `_edit` расширяется: обрабатывает джобу при `state ∈ {CREATED, EDITING}` (И `kind=='edit'`). Первый вход (`CREATED`) делает `transition(EDITING)` и идёт дальше; повторный вход после crash-resume (`EDITING`) переобрабатывает Agent 4 editor **идемпотентно**, не падая на guard'е. `task_edit` идемпотентен по построению: повторный editor-проход переписывает кандидат-ревизию текущей джобы (тот же `job_id`), `count_edit_start` идемпотентен по `job_id` ([ADR-014 §A](ADR-014-edit-limit-revision-rollback.md)).

**Crash-resume инвариант (нормативно):** после перехода в `EDITING` повторный вход (`acks_late`/retry/reconciler-ре-диспетчеризация) ОБЯЗАН снова попасть в `task_edit` (не в `task_fix`), идемпотентно переобработать Agent 4 editor. Держится двумя согласованными фактами: (1) `dispatch_for_state(EDITING) → task_edit`; (2) guard `_edit` принимает `EDITING`. `task_fix` недостижим для edit-джобы на этом этапе — edit-джоба не проходит `FIXING` на старте (она входит в build/deploy/FIXING-машинерию только **после** `EDITING → BUILDING`, и build-fail новой правки штатно уводит её в `FIXING → task_fix`, что корректно — это уже build-fix новой ревизии, как у generation).

### D. Reconciler-скоуп: `EDITING` — активное нетерминальное, ветвь fail-stuck (LLM-фаза)

`EDITING` добавляется в скоуп reconciler'а ([pipeline §E2](../modules/pipeline/03-architecture.md#e2-reconciler-застрявших-активных-состояний-crash-resume--concurrency-leak-guard-adr-019)) как активное нетерминальное состояние, удерживающее concurrency-слот. Stuck-набор: `CREATED, INTERVIEWING, SPECCING, EDITING, BUILDING, DEPLOYING, FIXING`.

- **`EDITING` — LLM-фазное** (Agent 4 editor — LLM-вызов, как INTERVIEWING/SPECCING), поэтому при провисании дольше `STUCK_THRESHOLD_S` без живой таски подпадает под **ветвь (2) fail-stuck** → `FAILED(stuck_timeout)` (тот же предохранитель concurrency-leak; при систематически недоступном LLM «повторная постановка» лишь бесконечно крутила бы Celery-retry). Согласовано с разграничением ветвей §E2: ветвь (1) ре-диспетчеризация — для resumable build/deploy/fixing; ветвь (2) fail-stuck — для LLM-фаз.
- Heartbeat: вход в `EDITING` двигает `last_transition_at` (любая смена `state` — heartbeat, [ADR-029](ADR-029-terminal-state-invariant-no-overwrite-deploy-guard-reconciler-revoke.md)) → джоба, реально прогрессирующая в editor'е, не получает ложный fail-stuck из-за провисания в `CREATED`.
- Терминальность ([ADR-029](ADR-029-terminal-state-invariant-no-overwrite-deploy-guard-reconciler-revoke.md)) не затрагивается: `EDITING` — нетерминал, переход в него и из него идёт через CAS-барьер `transition()` (предикат `state NOT IN ('LIVE','FAILED')`), `EDITING` под предикат не подпадает и пишется штатно. Wall-clock §C(c) применим как к любому активному state.

### E. Миграция (PG enum)

`state` — PG enum `job_state` (см. Context), поэтому ввод `EDITING` требует **новой Alembic-миграции** `ALTER TYPE job_state ADD VALUE 'EDITING'` (revises head `20260611_0001`). `ADD VALUE` аддитивен и не трогает существующие строки (backfill не нужен — `EDITING` присваивается только новым edit-джобам в рантайме). Порядок значения в enum не нормативен (диспетчеризация по равенству значения, не по ordinal). `Enum(JobState, name="job_state")` в моделях расширяется новым членом `JobState.EDITING`.

> **Замечание по `ALTER TYPE ... ADD VALUE` (требование к реализации миграции, зона backend/devops):** в PostgreSQL `ALTER TYPE ... ADD VALUE` **не может выполняться внутри транзакционного блока** (до PG12 строго; на PG12+ новое значение нельзя использовать в той же транзакции). Миграция обязана выполнить `ADD VALUE` так, чтобы это ограничение не нарушалось (например `op.execute` вне транзакции миграции / `COMMIT` перед использованием). Это нормативное требование к миграции, конкретная реализация — зона backend.
>
> **⚠️ Ревизовано [ADR-031](ADR-031-alembic-sync-engine-non-transactional-ddl.md) (прод-фикс 2026-06-12).** Это требование оказалось НЕДОСТАТОЧНЫМ: первая реализация миграции `20260612_0001` использовала `op.get_context().autocommit_block()`, но на проде DDL **не применился** — `autocommit_block()` поверх **async**-движка env.py (asyncpg+`run_sync`) НЕ переводит реальное соединение в AUTOCOMMIT (стандартный alembic-механизм рассчитан на **sync** psycopg-движок). Нормативный механизм теперь зафиксирован в [ADR-031](ADR-031-alembic-sync-engine-non-transactional-ddl.md): alembic-движок переведён на **sync psycopg** (`DATABASE_URL_SYNC`), где `autocommit_block()` работает штатно; паттерн non-transactional DDL — [03-data-model.md → Migration-guidance](../03-data-model.md). Миграция `20260612_0001` переписывается под этот паттерн + идемпотентность (`IF NOT EXISTS`).

## Consequences

- Новое значение enum `job_state` — `EDITING` (миграция `ALTER TYPE ADD VALUE`, revises `20260611_0001`); член `JobState.EDITING` в [enums.py](../../app/db/enums.py).
- Новый dispatcher-кейс `EDITING → task_edit`; коллизия `FIXING`-маршрута снята (edit-обработка не делит маршрут с fix-витком).
- Guard `_edit` расширен на `{CREATED, EDITING}` (crash-resume); `task_edit` остаётся идемпотентным.
- Reconciler-скоуп +`EDITING` (ветвь fail-stuck, LLM-фаза); stuck-набор: `CREATED, INTERVIEWING, SPECCING, EDITING, BUILDING, DEPLOYING, FIXING`.
- `EDITING` — **не** новый терминал и **не** новый failure_reason: терминалы остаются `{LIVE, FAILED}`, перечень `failure_reason` не расширяется (editor-провал по-прежнему `edit_failed_rolled_back` / гард-коды).
- State-machine edit-flow становится `CREATED → EDITING → BUILDING → DEPLOYING → LIVE` (mermaid + таблица + текст в [pipeline §B](../modules/pipeline/03-architecture.md#b-state-machine--расширение-sprint-2) приведены в согласование).
- ADR-014 §C (авто-rollback) и §A (точка инкремента `edit_usage`) не меняются: инкремент по-прежнему на успешном старте edit-джобы, идемпотентно по `job_id`; вход в `EDITING` — это и есть «старт активной обработки» edit-джобы (постановка/выполнение первой `task_edit`).
- Новых env-ключей нет.

## Alternatives

- **Вариант 2 — `FIXING` + kind-aware dispatch** (edit → `FIXING`; `dispatch_for_state(FIXING, kind=edit) → task_edit`, `kind=generation → task_fix`). Отвергнут: (1) **семантически перегружает `FIXING`** — смешивает fix-виток (build-fail recovery с `failure_log`) и edit-обработку (правка по instruction), что путает reconciler-скоуп, reason-коды и наблюдаемость (клиент/дашборд видит `FIXING` на штатной правке); (2) делает диспетчеризацию `FIXING` **kind-зависимой** — усложняет инвариант «лейбл перехода = state = task по state» (для `FIXING` появляется развилка по `kind`), тогда как вариант 1 держит однозначный маршрут на состояние; (3) не даёт отдельного user-facing статуса «обработка правки» (видимый статус был бы `FIXING` — вводит в заблуждение). Плюс варианта 2 (нет миграции enum) перевешен ценой смешения семантики. Миграция `ADD VALUE` — аддитивна и дёшева.
- **Оставить `CREATED`, добавить лишь промежуточное `job_events`-событие без смены `state`** (например `edit_processing`). Отвергнут: не двигает `last_transition_at` (ложный fail-stuck остаётся), и клиенты, читающие `state` (`GET /jobs/{id}`, SSE-снимок), всё равно видят `CREATED` — UX-баг не закрыт. Видимый прогресс в этой системе несёт `state`, а не только события.
- **Переиспользовать `INTERVIEWING`/`SPECCING` как «обработка»** — отвергнут: эти состояния семантически принадлежат generation-flow (Agent 1/Agent 2) и их диспетчеризация (`task_interview`/`task_spec`) не та; переиспользование сломало бы маршрутизацию.
