# 03 — Data Model (Postgres 16)

Принцип: **Postgres — system of record для метаданных. Все крупные бинарные артефакты (исходники `source.tgz`, `dist/`, build-логи) — в S3/MinIO, в БД хранятся только ссылки (`*_ref` = S3-ключ).** Adapty — источник истины по подпискам, `subscriptions` — локальный кэш.

ID: префиксные opaque строки (`u_`, `p_`, `j_`, `r_`, `d_`). Все таймстампы — `timestamptz` (UTC). Денежные величины — `numeric(10,4)` USD.

## ER-диаграмма

```mermaid
erDiagram
    users ||--o{ api_tokens : authenticates_via
    users ||--o{ projects : owns
    users ||--o{ generation_jobs : owns_denorm
    users ||--o{ subscriptions : has
    users ||--o{ usage_counters : tracks
    users ||--o{ edit_usage_counters : tracks_edits
    users ||--o{ credit_grants : granted_bonus
    users ||--o{ device_tokens : registers
    users ||--o{ billing_events : receives
    projects ||--o{ generation_jobs : has
    projects ||--o{ revisions : has
    projects ||--o| revisions : current_revision
    generation_jobs ||--o{ job_events : logs
    generation_jobs ||--o{ questions : asks
    questions ||--o{ answers : answered_by
    generation_jobs ||--o{ llm_usage : costs
    generation_jobs ||--o| site_deployments : deploys
    revisions ||--o| site_deployments : deployed_as
    plan_quotas ||--o{ subscriptions : maps_access_level
```

## users

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `u_...`. Используется как Adapty `customer_user_id` ([Q-BILLING-3](99-open-questions.md#q-billing-3)). |
| `apple_sub` | text NULL UNIQUE | **Sprint 3.** Стабильный `sub` из Apple identity token (Sign in with Apple, [ADR-007](adr/ADR-007-sign-in-with-apple.md)). Identity-якорь upsert'а пользователя. NULL допустим только для legacy S1 seeded-юзера. UNIQUE-индекс. |
| `api_key_hash` | text NULL | **Legacy (S1).** argon2id-хэш единственного seeded Bearer-ключа. С Sprint 3 реальные токены живут в `api_tokens` (мульти-устройство, индексируемый lookup). Поле сохранено на время миграции (fallback-путь, [ADR-008](adr/ADR-008-indexed-api-key-lookup.md) → «Миграционный путь»); становится nullable. |
| `auth_secret_hash` | text NULL | **[ADR-024] — клиентская аутентификация по `user_id`+секрет.** argon2id-хэш секрета пользователя для `POST /v1/auth/login` (вход без Apple/админ-ключа). Сам секрет не хранится/не восстановим; constant-time verify. **Один секрет на юзера** (поле на `users`, не отдельная таблица — [ADR-024 §3](adr/ADR-024-user-id-secret-authentication.md)). **`NULL`** у Apple-юзеров (вход через `/auth/apple`) и admin-created (login-as) — у них секрета нет; login по секрету для них → единый `401`. **Без UNIQUE** — `auth_secret_hash` **не** identity-якорь (им остаётся `id`/`apple_sub`), это лишь верификатор; UNIQUE по хэшу не имеет смысла. Set/rotate — `POST /v1/auth/secret` (под Bearer). Никогда не логируется (как `key_hash`). Миграция — `add_column users.auth_secret_hash` (revises head `20260604_0001`). |
| `adapty_customer_user_id` | text NULL UNIQUE | Связка с Adapty профилем = `users.id`. Создаётся при первом входе iOS ([Q-BILLING-3](99-open-questions.md#q-billing-3) resolved). |
| `monthly_budget_usd` | numeric(10,4) | Технический потолок затрат Claude на юзера (отдельно от бизнес-квоты). |
| `bonus_generations_balance` | int NOT NULL default 0 | **ADR-021.** Накопительный баланс бонус-генераций (кредитов), начисляемых админом **сверх** плановой месячной квоты ([ADR-021 §D](adr/ADR-021-admin-plane-and-bonus-credits.md)). **НЕ обнуляется помесячно** (в отличие от `usage_counters.generations_used`, ключуемого `period`). Денормализованный O(1)-баланс для quota-gate/`billing/me`; источник истины величины — эта колонка (атомарно мутируется), история начислений — append-only `credit_grants`. **Инвариант:** `>= 0` (отрицательная коррекция не уводит ниже 0). Списание на старте generation-джобы **только после** исчерпания плановой квоты (`usage_counters.generations_used >= monthly_generations`) — [modules/billing/03-architecture.md §10](modules/billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021). Миграция `20260604_0001`. |
| `status` | text | `active` / `suspended`. |
| `created_at` | timestamptz | |

> **ADR-021 (login-as без Apple):** `apple_sub=NULL` теперь допустим **не только** для legacy S1 seed-юзера, но и для юзеров, созданных через `POST /v1/admin/login-as` (выпуск пользовательского токена за `user_id` без Sign in with Apple, [ADR-021 §B](adr/ADR-021-admin-plane-and-bonus-credits.md)). UNIQUE-индекс по `apple_sub` сохраняется (NULL не нарушает UNIQUE в Postgres). `adapty_customer_user_id = users.id` проставляется и для admin-created юзеров (как при Apple-входе).

> **ADR-024 (register без Apple):** юзеры, созданные через `POST /v1/auth/register`, также имеют `apple_sub=NULL` (как admin-created) + заполненный `auth_secret_hash`. **Миграция (вход для backend):** аддитивный `add_column users.auth_secret_hash text NULL` (без backfill — существующие Apple/admin-юзеры остаются с `NULL`), **revises текущий head** `20260604_0001`. Поле nullable и без UNIQUE → миграция не ломает существующие строки. Имя ревизии и точная DDL — backend по конвенции Alembic-чейна (`down_revision = "20260604_0001"`).

## api_tokens (Sprint 3)

Реальные opaque Bearer-токены пользователя. **Мульти-устройство:** N активных строк на `user_id` (по токену на устройство). Индексируемый O(1) lookup по `key_id` ([ADR-008](adr/ADR-008-indexed-api-key-lookup.md)) — закрывает [TD-004](100-known-tech-debt.md#td-004).

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `t_...`. Адресует токен в `DELETE /v1/auth/tokens/{id}`. |
| `user_id` | text FK→users NOT NULL | Владелец. Индекс по `(user_id)` для листинга устройств. |
| `key_id` | text NOT NULL UNIQUE | **Публичный** индексируемый префикс ключа (`[a-z0-9]{16}`, не секрет). Точка O(1) lookup: `WHERE key_id = :key_id AND revoked_at IS NULL`. UNIQUE-индекс — **единственная** точка поиска токена ([ADR-008](adr/ADR-008-indexed-api-key-lookup.md)). |
| `key_hash` | text NOT NULL | argon2id-хэш **секретной** части ключа. Сам секрет не хранится/не восстановим. Один constant-time argon2-verify после lookup по `key_id`. |
| `device_label` | text NULL | Опц. человекочитаемая метка устройства (для UI списка устройств). |
| `created_at` | timestamptz NOT NULL | Момент выдачи. |
| `last_used_at` | timestamptz NULL | Обновляется при успешной аутентификации (для UI/аудита; апдейт best-effort, вне горячей транзакции). |
| `revoked_at` | timestamptz NULL | NULL = активен. Отзыв (`DELETE /v1/auth/tokens/{id}`/logout) выставляет `now()` — мягкий revoke без удаления строки (аудит). Lookup игнорирует `revoked_at IS NOT NULL`. |

> Формат ключа, выдаваемого клиенту: `lv_<key_id>_<secret>` ([ADR-008](adr/ADR-008-indexed-api-key-lookup.md)). В БД — только `key_id` (открыто) + argon2-хэш `secret`. Миграция `api_tokens` вводится в Sprint 3.

## projects

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `p_...` |
| `user_id` | text FK→users | Изоляция: все запросы фильтруются по `user_id`. |
| `prompt` | text | Исходный промт пользователя. |
| `current_revision_id` | text FK→revisions NULL | Текущая «good» ревизия (для rollback). |
| `title` | text NULL | Опц. человекочитаемое имя. |
| `created_at` | timestamptz | |
| `deleted_at` | timestamptz NULL | **Sprint 4.** Soft-delete-маркер ([ADR-011](adr/ADR-011-project-delete-gc.md)). `NULL` = активен. `DELETE /projects/{pid}` ставит `now()` → проект исключается из всех `GET`-листингов/деталей (фильтр `deleted_at IS NULL`) и из подсчёта `max_projects` quota-gate (`projects_used` считает только `deleted_at IS NULL`); ставится Celery `project.gc` (полный GC ресурсов → hard-delete строки). Миграция `20260602_0003`. |

## generation_jobs

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `j_...` |
| `project_id` | text FK→projects | |
| `user_id` | text FK→users NOT NULL | Денормализация владельца (= `projects.user_id`). Нужен для unique-constraint идемпотентности и фильтрации tenant-изоляции без join. Индекс по `(user_id)`. |
| `state` | enum NOT NULL | PG enum `job_state` (`Enum(JobState, name="job_state")`, [app/db/models.py](app/db/models.py); `postgresql.ENUM(...)` в [migrations/versions/20260602_0001_initial_s1.py](migrations/versions/20260602_0001_initial_s1.py)). Значения: `CREATED, INTERVIEWING, AWAITING_CLARIFICATION, SPECCING, BUILDING, DEPLOYING, LIVE, FIXING, FAILED, EDITING`. **Диспетчер маршрутизирует по этой колонке.** Индекс по `(state)`. **`EDITING` ([ADR-030](adr/ADR-030-editing-visible-state-edit-job.md))** — видимый промежуточный статус edit-джобы (Agent 4 editor), активное нетерминальное LLM-фазное состояние; маршрут `EDITING → task_edit` ([modules/pipeline/03-architecture.md → Диспетчер-таблица](modules/pipeline/03-architecture.md#нормативная-таблица-dispatch_for_state-state--task)). **Миграция:** новая Alembic-миграция `ALTER TYPE job_state ADD VALUE 'EDITING'` (revises head `20260611_0001`); аддитивна, backfill не нужен (значение присваивается только новым edit-джобам в рантайме). `ADD VALUE` в PG нельзя выполнять/использовать в одной транзакции — миграция исполняет его вне транзакционного блока (требование к реализации, зона backend, [ADR-030 §E](adr/ADR-030-editing-visible-state-edit-job.md)). |
| `kind` | text | `generation` / `edit` (post-delivery правка) / `rollback` (**Sprint 5** — джоба re-deploy сохранённой good-ревизии, ручной `POST .../rollback`; [ADR-014 §B](adr/ADR-014-edit-limit-revision-rollback.md), [modules/deploy/03-architecture.md §7](modules/deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014)). `kind='rollback'`-джоба **не** проходит через `FIXING` — это прямой re-deploy `is_good`-ревизии (`BUILDING/DEPLOYING → LIVE`), без Agent 4 / fix-loop. Не инкрементирует ни `usage_counters`, ни `edit_usage_counters` ([ADR-014 §A](adr/ADR-014-edit-limit-revision-rollback.md)). |
| `idempotency_key` | text NULL | Партиальный UNIQUE-индекс `(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL`. Опирается на денормализованный `user_id` (см. выше). Для дедупа `POST /projects` и `/edits`. |
| `retry_count` | int | Текущая глубина fix-loop. Правило инкремента — единственный нормативный источник [pipeline §C(a)](modules/pipeline/03-architecture.md#c-четыре-гарда-от-бесконечного-цикла-и-runaway-затрат): инкремент **на каждом применённом патче** (`FIXING → BUILDING`), не на невалидном патче и не на инфра-ретраях Celery ([ADR-006](adr/ADR-006-celery-retry-vs-domain-fixing.md)). Гард (a). |
| `max_fix_attempts` | int | Hard cap (a). Инициализируется из env `MAX_FIX_ATTEMPTS` (default 3) при создании джобы. |
| `budget_usd` | numeric(10,4) | Cost cap джобы (b). Инициализируется из env `JOB_BUDGET_USD` (в S3.5 — per-plan `plan_quotas.job_budget_usd`). |
| `spend_usd` | numeric(10,4) | Накопленные затраты (сумма `llm_usage.cost_usd`). |
| `wall_clock_deadline` | timestamptz NULL | Wall-clock cap (c). `created_at + JOB_WALL_CLOCK_BUDGET_S` (default 3600 s). NULL ⇒ гард выключен. |
| `last_failure_signature` | text NULL | **Внутреннее guard-state.** Хэш сигнатуры предыдущего фейла для no-progress detection (d). Алгоритм — [ADR-005](adr/ADR-005-no-progress-failure-signature.md). |
| `failure_event_pending` | boolean NOT NULL default false | **Внутреннее guard-state** (как `last_failure_signature`). Различает новый distinct failure-event от crash-resume reprocessing того же лога reconciler'ом в no-progress гарде (d). Выставляется при рождении нового failure-event (`enter_fixing` и обработка невалидного патча Agent 4), сбрасывается гардом no-progress. Семантика — [pipeline §C(d)](modules/pipeline/03-architecture.md#d-no-progress-detection); миграция `20260602_0002`. Уточняет [ADR-005](adr/ADR-005-no-progress-failure-signature.md). |
| `content_language` | text NOT NULL default `'en'` | **BCP-47 язык контента сайта ([ADR-028](adr/ADR-028-deterministic-source-prompt-language-detection.md) ревизует [ADR-025](adr/ADR-025-content-language-autodetect-spec-marker.md)).** Детерминированный серверный детект из **исходного** `project.prompt` (script-эвристика) **один раз** на старте фазы interview (`task_interview`), **до** Agent 1. Crash-устойчивый якорь языка: переживает рестарт воркера между фазами, восстанавливается **без передетекта**; единый источник для серверной language-директивы, инжектируемой в Agent 1/Agent 2. Значение маркера `**Content language:**` в `spec_markdown` (downstream Agent 3/4) приходит отсюда, а не из само-детекта LLM. Fallback при неуверенном/смешанном script — `'en'` (= default). Нормативная механика — [pipeline §Язык/локализация](modules/pipeline/03-architecture.md#языклокализация-контента-сайта--детерминированный-детект-adr-028-ревизует-adr-025). **Миграция:** новая `20260611_0001` (revises head `20260608_0001`); backfill существующих строк значением `'en'` (NOT NULL default). |
| `spec_tz` | text NULL | Финальная спека (output Agent 2). Если большая — `spec_ref` в S3 (решение: текст ≤ 16 KB inline, иначе ref). |
| `failure_log_ref` | text NULL | S3-ключ лога фейла **текущей попытки** (per-attempt, [ADR-022](adr/ADR-022-per-attempt-build-logs.md)): build-стадия → `logs/{job_id}/build.{retry_count}.log`, deploy/health-стадия → `logs/{job_id}/deploy.{retry_count}.log`, отклонённый патч Agent 4 (`agent_output_invalid`, `_handle_invalid_patch`) → `logs/{job_id}/agent.{retry_count}.log`. Вход Agent 4. **Не** перезаписывает логи прежних попыток — каждая попытка пишет в уникальный ключ по монотонному `retry_count`; build/deploy/agent одной попытки — три разных имени-стадии (отклонённый патч пишется с тем же `retry_count`, т.к. `_handle_invalid_patch` его не инкрементирует, но в отдельный ключ — не затирая build/deploy того же витка). На каждом витке `failure_log_ref` указывает на лог именно этой попытки/стадии (история ранних фейлов сохранена в S3 + в `job_events`). Нормативно — [07-deployment.md → модель хранения](07-deployment.md#модель-хранения-один-бакет--key-префиксы), [modules/deploy/03-architecture.md §F-1](modules/deploy/03-architecture.md#f-1-per-attempt-ключи-builddeploy-логов-adr-022). |
| `failure_reason` | text NULL | Машинный код при `FAILED`. Перечень S2: `build_unrecoverable`, `budget_exhausted`, `wall_clock_exceeded`, `no_progress`, `fixer_gave_up`, `invalid_agent_output`, `infra_error`, `clarification_timeout` ([modules/pipeline/03-architecture.md → §C](modules/pipeline/03-architecture.md#c-четыре-гарда-от-бесконечного-цикла-и-runaway-затрат)). **S4:** добавлен `project_deleted` — джоба отменена удалением проекта ([ADR-011](adr/ADR-011-project-delete-gc.md), [modules/deploy/03-architecture.md §6](modules/deploy/03-architecture.md#6-gc-при-удалении-проекта-sprint-4--delete-projectsid-adr-011-закрывает-td-003q-deploy-3)). **S5:** добавлен `edit_failed_rolled_back` — edit-джоба (`kind=edit`) исчерпала гарды, откатилась на прежнюю `is_good`-ревизию; падает **только** edit-джоба, сайт остаётся `LIVE` на прежней ревизии ([ADR-014 §C](adr/ADR-014-edit-limit-revision-rollback.md), [modules/pipeline/03-architecture.md → post-delivery edit](modules/pipeline/03-architecture.md#post-delivery-edit-live--fixing--live--контракт-зафиксирован-реализация-в-sprint-5)). Без расширения enum `state` — терминал `FAILED`. **S5 (rollback):** провал re-deploy при ручном rollback (`kind='rollback'`) переиспользует существующий `infra_error` — новый reason-код **не** вводится (re-deploy сохранённой good-ревизии без LLM/новой сборки дерева; фейл = инфра/health, не доменный build-fail). Здоровье прежнего деплоя не затрагивается — прежняя good-ревизия остаётся `active` ([ADR-014 §B](adr/ADR-014-edit-limit-revision-rollback.md), [modules/deploy/03-architecture.md §7](modules/deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014)). **Prod-фикс ([ADR-019](adr/ADR-019-reconciler-all-active-states-agent-graceful-fail.md)):** добавлены `agent_unavailable` (LLM недоступен — `429`/`5xx`/timeout исчерпали ретраи **или** `401`/`403`/`400` без ретрая → graceful-fail шага агента, [pipeline §G](modules/pipeline/03-architecture.md#g-graceful-fail-шага-агента-при-недоступности-llm-adr-019)) и `stuck_timeout` (reconciler терминализировал джобу, провисевшую в активном LLM-фазном state дольше `STUCK_THRESHOLD_S` без живой таски — предохранитель concurrency-leak, [pipeline §E2](modules/pipeline/03-architecture.md#e2-reconciler-застрявших-активных-состояний-crash-resume--concurrency-leak-guard-adr-019)). |
| `last_transition_at` | timestamptz NOT NULL default now() | **[ADR-019] — heartbeat прогресса джобы.** Момент последнего входа в текущий `state`. Обновляется транзакционно при **каждой** смене `state` (та же транзакция, что `state`+`job_events`+publish), и **только** при ней — прочие апдейты строки (`spend_usd` cost-ledger, `failure_log_ref`, guard-state) его **не** трогают. Reconciler (§E2) использует именно его (а не `updated_at`, который дёргается cost-ledger'ом и ложно сбрасывал бы heartbeat) для stuck-критерия активных нетерминальных состояний → fail-stuck/ре-диспетчеризация против concurrency-leak. Нормативно — [modules/pipeline/03-architecture.md → §E2](modules/pipeline/03-architecture.md#e2-reconciler-застрявших-активных-состояний-crash-resume--concurrency-leak-guard-adr-019). Миграция: новая (backfill существующих строк значением `updated_at`). |
| `created_at` / `updated_at` | timestamptz | |

> Терминальные/устойчивые состояния, где задач в очереди нет: `AWAITING_CLARIFICATION`, `LIVE`, `FAILED`. Sweeper (beat) экспайрит `AWAITING_CLARIFICATION` по TTL.

> **Текст instruction правки (`POST /edits`) — отдельной колонки нет (Sprint 5).** `generation_jobs` **не** несёт колонки `instruction`: текст правки хранится в append-only `job_events` как `payload` события `edit_requested` (`event_type='edit_requested'`). `job_events` — уже источник истины событий джобы (аудит + вход SSE), edit-instruction естественно ложится туда без новой колонки/миграции. Это **единственный** нормативный источник хранения instruction; вход Agent 4 в edit-цикле читает её оттуда. Нормативно — [modules/pipeline/03-architecture.md → post-delivery edit](modules/pipeline/03-architecture.md#post-delivery-edit-live--fixing--live--контракт-зафиксирован-реализация-в-sprint-5), форма payload — [job_events](#job_events).

## job_events

Аудит + источник для SSE. Append-only.

| Поле | Тип | Заметки |
|---|---|---|
| `id` | bigserial PK | |
| `job_id` | text FK→generation_jobs | Индекс по `(job_id, id)`. |
| `event_type` | text | `state_changed`, `agent_started`, `question_posted`, `build_failed`, `fix_attempted`, `deployed`, `failed`, `edit_requested`, ... Для fix-loop: `build_failed` (с `payload.failure_class`/`failure_signature`), `fix_attempted` (с `payload.retry_count`). **Sprint 5:** `edit_requested` (`kind='edit'`) несёт в `payload` текст правки пользователя (`payload.instruction`) — **единственный** источник хранения instruction (отдельной колонки `generation_jobs.instruction` нет, см. примечание под `generation_jobs`). Несёт историю витков: `build_failed.payload.failure_log_ref` (build/deploy-фейл) и `fix_rejected.payload.failure_log_ref` (отклонённый патч Agent 4 → `agent.{retry_count}.log`) каждого события указывают на per-attempt лог именно той попытки/стадии ([ADR-022](adr/ADR-022-per-attempt-build-logs.md)) — `generation_jobs.failure_log_ref` хранит лишь ссылку **последней** записанной попытки/стадии, а полная история per-attempt ссылок восстанавливается из append-only `build_failed`/`fix_rejected`-событий. |
| `from_state` / `to_state` | text NULL | Для переходов. |
| `payload` | jsonb | Доп. данные события. |
| `created_at` | timestamptz | |

## questions / answers

| `questions` | Тип | Заметки |
|---|---|---|
| `id` | text PK | |
| `job_id` | text FK→generation_jobs | |
| `position` | int | Порядок. |
| `text` | text | Текст вопроса (output Agent 1). |
| `kind` | text NULL | `choice` / `free_text` (опц. для UI). |
| `options` | jsonb NULL | Варианты для `choice`. |

| `answers` | Тип | Заметки |
|---|---|---|
| `id` | text PK | |
| `question_id` | text FK→questions | |
| `job_id` | text FK→generation_jobs | Денормализация для резюма. |
| `text` | text | Ответ пользователя. |
| `created_at` | timestamptz | |

## revisions

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `r_...` |
| `project_id` | text FK→projects | |
| `revision_no` | int | Монотонный per-project. UNIQUE `(project_id, revision_no)`. Адресует ревизию в `POST /projects/{pid}/revisions/{revision_no}/rollback` (**Sprint 5**, [ADR-014](adr/ADR-014-edit-limit-revision-rollback.md)). |
| `source_artifact_ref` | text | S3-ключ `source.tgz` этой ревизии. **Sprint 5:** источник пересборки при rollback, если `dist` целевой ревизии недоступен ([ADR-014 §B](adr/ADR-014-edit-limit-revision-rollback.md)). |
| `created_from_job_id` | text FK→generation_jobs | Какая джоба породила ревизию (генерация или правка `kind=edit`). |
| `is_good` | bool | `true` = успешно задеплоенная ревизия. **Sprint 5:** rollback (ручной — `POST .../rollback`; авто — при неудачной правке) откатывает `projects.current_revision_id` на ревизию с `is_good=true` (передеплой готового `dist` или пересборка из `source_artifact_ref`). Rollback **не** создаёт новую ревизию и **не** меняет `is_good` существующих ([ADR-014](adr/ADR-014-edit-limit-revision-rollback.md)). |
| `created_at` | timestamptz | |

> **Rollback ([ADR-014](adr/ADR-014-edit-limit-revision-rollback.md), Sprint 5):** rollback меняет, какая good-ревизия активна (`projects.current_revision_id`), переиспуская deploy-lifecycle: новый деплой целевой ревизии подтверждается health `200` → прежний `active`-деплой `→ superseded` (teardown). Health-fail нового деплоя оставляет прежнюю ревизию активной (без downtime). Нормативно — [modules/api/02-api-contracts.md → rollback](modules/api/02-api-contracts.md#post-projectspidrevisionsrevision_norollback-sprint-5), [modules/deploy/03-architecture.md §7](modules/deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014).

## site_deployments

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `d_...` |
| `project_id` | text FK→projects | |
| `revision_id` | text FK→revisions | |
| `subdomain` | text UNIQUE | Opaque-идентификатор деплоя (`[a-z0-9]{16}`, **не** `project_id`), генерируется при деплое. **Single normative source идентификатора деплоя**; колонка не переименовывается. В режиме `SITE_ROUTING_MODE=subdomain` — хост `{subdomain}.apps.domain`; в режиме `path` ([ADR-017](adr/ADR-017-path-based-site-routing.md)) то же значение служит сегментом пути `site_id` → `{APPS_DOMAIN}/s/{site_id}`. Единый для Traefik router rule, `live_url` и health-check (в обоих режимах). Устойчив к смене проекта/ревизии, opaque и **не реюзается** (защита от takeover). |
| `live_url` | text | Режим `subdomain`: `https://{subdomain}.apps.domain/`. Режим `path` ([ADR-017](adr/ADR-017-path-based-site-routing.md)): `https://{APPS_DOMAIN}/s/{site_id}/` (со слешем). Нормативный формат по режиму — [modules/deploy/03-architecture.md §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017). |
| `dist_artifact_ref` | text | S3-ключ собранного `dist/`. |
| `build_log_ref` | text NULL | S3-ключ build-лога **попытки**, на которой собрался задеплоенный `dist` (per-attempt, [ADR-022](adr/ADR-022-per-attempt-build-logs.md)): `logs/{job_id}/build.{retry_count}.log`. **Не** перезаписывается прежними попытками. |
| `container_id` | text NULL | ID nginx-контейнера. Имя контейнера детерминировано (`site_{subdomain}`) — основа cleanup-before-run и teardown. GC при удалении проекта — [Q-DEPLOY-3](99-open-questions.md#q-deploy-3). |
| `status` | text | Машина состояний деплоя: `building` / `active` / `superseded` / `failed`. Полный lifecycle, легальные переходы и обязательный teardown на фейловых/вытесняющих переходах — [modules/deploy/03-architecture.md → §5 Lifecycle сайт-деплоя](modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus). **`torn_down` переименован в `failed`** (deploy не прошёл health-gate и снесён); «снесён при вытеснении новой ревизией» = `superseded`. |
| `created_at` | timestamptz | |

## llm_usage (cost-ledger)

| Поле | Тип | Заметки |
|---|---|---|
| `id` | bigserial PK | |
| `job_id` | text FK→generation_jobs | Индекс. |
| `agent` | text | `agent1`..`agent4`. |
| `model` | text | Использованная модель Claude. |
| `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` | int | Учёт prompt caching. |
| `cost_usd` | numeric(10,4) | Себестоимость вызова. |
| `created_at` | timestamptz | |

---

## Биллинг

### subscriptions (локальный кэш Adapty)

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | |
| `user_id` | text FK→users | |
| `access_level` | text | Маппится на `plan_quotas.access_level` (`free`/`pro`). Источник истины — Adapty. Нет строки `subscriptions` ⇒ трактуется как `free`. |
| `product_id` | text NULL | Adapty product (привязка product_id↔access_level — в дашборде Adapty, внешняя зависимость; `plan_quotas` ключуется по `access_level`). |
| `status` | text | `active` / `expired` / `grace` / `billing_issue`. На гейте (quota-gate) пропускаются **только** `active` и `grace`; `billing_issue`/`expired` → `402` ([modules/billing/03-architecture.md §4](modules/billing/03-architecture.md#4-entitlements--quota-gate)). State-machine переходов по `event_type` — единственный нормативный источник [modules/billing/03-architecture.md §2.3/§6](modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица). Политика сайтов при `expired`/refund: grace `GRACE_PERIOD_DAYS` (7) → teardown ([08-product-decisions.md §3.5-6](08-product-decisions.md#sprint-35--billing-adapty), [Q-BILLING-1](99-open-questions.md#q-billing-1)). |
| `store` | text | `app_store` / ... |
| `started_at` / `expires_at` | timestamptz | |
| `grace_until` | timestamptz NULL | Дедлайн grace-периода (`expire/refund + GRACE_PERIOD_DAYS`). `subscription_sweep` (beat) сносит сайты при `status='grace' AND grace_until < now()`. Renew/started в grace → `grace_until=NULL` (teardown отменён). NULL вне grace. [modules/billing/03-architecture.md §6](modules/billing/03-architecture.md#6-grace-период-сайтов-q-billing-1). |
| `will_renew` | bool | |
| `adapty_transaction_id` | text | |
| `raw` | jsonb | Сырой профиль/событие Adapty. |
| `synced_at` | timestamptz | Последняя ресинхронизация (`getProfile`). TTL свежести — `BILLING_RESYNC_INTERVAL_S`; протух ⇒ lazy-ресинк на гейте/`/billing/me`. |

### plan_quotas

| Поле | Тип | Заметки |
|---|---|---|
| `access_level` | text PK | `free` / `pro` (premium = `pro`, [Q-BILLING-1](99-open-questions.md#q-billing-1) resolved). |
| `monthly_generations` | int | Бизнес-квота генераций/мес. |
| `max_concurrent_jobs` | int | Cap конкурентных активных джоб (энфорс — quota-gate; в S3 rate-limit/cap опираются на дефолт free до подключения billing). Счёт активных джоб **kind-агностичен** (`generation`/`edit`/`rollback` все занимают слот) — нормативный источник [modules/billing/03-architecture.md §4.3](modules/billing/03-architecture.md#4-entitlements--quota-gate); следствие на Free (`=1`) и наблюдаемость — [TD-012](100-known-tech-debt.md#td-012). |
| `max_projects` | int NULL | `NULL` = безлимит (Pro). |
| `job_budget_usd` | numeric(10,4) | Технический потолок Claude на джобу для тарифа. |
| `monthly_edits` | int NULL | **Sprint 5** ([ADR-014](adr/ADR-014-edit-limit-revision-rollback.md)). Бизнес-квота **правок** (`kind='edit'`)/мес — **отдельная** от `monthly_generations` ([08 §5-2](08-product-decisions.md#sprint-5--realtime--edits)). `NULL` = безлимит (Pro). Энфорс — quota-gate на `/edits` против `edit_usage_counters.edits_used`. |

**Сидинг `plan_quotas` (нормативная таблица — [08-product-decisions.md → Sprint 3.5](08-product-decisions.md#sprint-35--billing-adapty) / [§5-2](08-product-decisions.md#sprint-5--realtime--edits)):**

| `access_level` | `monthly_generations` | `max_projects` | `max_concurrent_jobs` | `job_budget_usd` | `monthly_edits` |
|---|---|---|---|---|---|
| `free` | 3 | 1 | 1 | 5.0000 (env `JOB_BUDGET_USD`) | 5 |
| `pro` | 100 | `NULL` (безлимит) | 3 | 5.0000 (env `JOB_BUDGET_USD`) | `NULL` (безлимит) |

> `monthly_edits` сидится Alembic data-migration **Sprint 5** (дополняет существующие строки Free/Pro). Значения — [08 §5-2](08-product-decisions.md#sprint-5--realtime--edits).

> Две независимые величины: бизнес-квота (`monthly_generations`) vs себестоимость (`job_budget_usd`). Значения тарифов — [Q-BILLING-1](99-open-questions.md#q-billing-1) (resolved); себестоимость/калибровка — [Q-COST-1](99-open-questions.md#q-cost-1) (Sprint 6).

### usage_counters

| Поле | Тип | Заметки |
|---|---|---|
| `user_id` | text FK→users | PK `(user_id, period)`. |
| `period` | text | `YYYY-MM`. |
| `generations_used` | int | Инкремент на **успешный старт генерации** (`kind='generation'`), не на `POST /projects` и не на `/answers`. Атомарный upsert `ON CONFLICT (user_id, period) DO UPDATE`, идемпотентно по `job_id` (guard от двойного инкремента при Celery-реплее). Точка инкремента — нормативно [modules/billing/03-architecture.md §5](modules/billing/03-architecture.md#5-учёт-usage_counters). Сверяется с `plan_quotas.monthly_generations` на гейте. Правки (`kind='edit'`, S5) **не** инкрементируют этот счётчик — у них отдельный `edit_usage_counters` ([ADR-014](adr/ADR-014-edit-limit-revision-rollback.md), [08 §5-2](08-product-decisions.md#sprint-5--realtime--edits)). |

### edit_usage_counters (Sprint 5)

Отдельный помесячный счётчик **правок** (`kind='edit'`) — лимит правок независим от квоты генераций ([ADR-014](adr/ADR-014-edit-limit-revision-rollback.md), [08 §5-2](08-product-decisions.md#sprint-5--realtime--edits)). Структурно зеркалит `usage_counters`.

| Поле | Тип | Заметки |
|---|---|---|
| `user_id` | text FK→users | PK `(user_id, period)`. |
| `period` | text | `YYYY-MM` (UTC). |
| `edits_used` | int | Инкремент на **успешный старт edit-джобы** (`kind='edit'`, постановка первой `task_fix`-edit), **не** на `POST /edits` и **не** на rollback. Атомарный upsert `ON CONFLICT (user_id, period) DO UPDATE`, идемпотентно по `job_id`. Сверяется с `plan_quotas.monthly_edits` на quota-gate `/edits`. Нормативная точка — [modules/billing/03-architecture.md §7](modules/billing/03-architecture.md#7-граница-s5-edits). |

> Rollback (`POST .../rollback`) **не** инкрементирует `edit_usage_counters` — это передеплой существующей good-ревизии без новой генерации/правки ([ADR-014 §A](adr/ADR-014-edit-limit-revision-rollback.md)).

### credit_grants (бонус-генерации, ADR-021)

Append-only ledger начислений/коррекций бонус-генераций (кредитов) админом ([ADR-021 §D](adr/ADR-021-admin-plane-and-bonus-credits.md)). Аудит-история + точка идемпотентности начисления. Текущий баланс — денормализован в `users.bonus_generations_balance` (источник истины величины); ledger хранит **историю изменений**, не сам остаток.

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `cg_...`. |
| `user_id` | text FK→users NOT NULL | Получатель кредитов. Индекс по `(user_id)`. |
| `amount` | int NOT NULL | Дельта баланса: `> 0` — начисление, `< 0` — операторская коррекция/списание. Применение `amount` к `users.bonus_generations_balance` — атомарно в одной транзакции с insert этой строки; результирующий баланс **не может стать < 0** (`409` при попытке увести в минус — [modules/admin/02-api-contracts.md §3](modules/admin/02-api-contracts.md)). |
| `reason` | text NULL | Опц. человекочитаемая причина начисления (операторская заметка, аудит). |
| `idempotency_key` | text NULL | Партиальный UNIQUE-индекс `(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL`. Дедуп начисления по заголовку `Idempotency-Key` (`POST /v1/admin/users/{user_id}/credits`): повтор с тем же ключом → no-op (строка не дублируется), возврат текущего баланса. Без ключа — каждый вызов = новая строка. |
| `created_by` | text NOT NULL default 'admin' | Кто начислил. Сейчас единственный источник — админ (`ADMIN_API_KEY`); per-operator-аудит — отдельный ADR при необходимости ([ADR-021 §Consequences](adr/ADR-021-admin-plane-and-bonus-credits.md)). |
| `created_at` | timestamptz NOT NULL | Момент начисления. |

> **Списание кредитов НЕ создаёт строку `credit_grants`.** Расход кредита на старте generation-джобы — атомарный декремент `users.bonus_generations_balance` (как `usage_counters` для плановой квоты), не ledger-запись. `credit_grants` фиксирует **только** админские начисления/коррекции (входящий поток), не помесячный расход. Семантика списания — [modules/billing/03-architecture.md §10](modules/billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021). Миграция `20260604_0001`.

### device_tokens (Sprint 5)

APNs device tokens для push-нотификаций ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)). **Мульти-устройство:** N токенов на user.

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `dev_...`. |
| `user_id` | text FK→users NOT NULL | Владелец. Индекс по `(user_id)` для выборки устройств при отправке push. |
| `apns_token` | text NOT NULL | APNs device token (hex). UNIQUE `(user_id, apns_token)` — upsert при регистрации `POST /v1/devices`. |
| `platform` | text NOT NULL | `ios` (зарезервировано на будущие платформы). |
| `environment` | text NOT NULL | `sandbox` / `production` — определяет APNs-хост (`api.sandbox.push.apple.com` / `api.push.apple.com`). |
| `created_at` | timestamptz NOT NULL | Момент регистрации. |
| `last_push_at` | timestamptz NULL | Последняя успешная доставка (аудит, best-effort). |
| `invalidated_at` | timestamptz NULL | NULL = активен. Выставляется `now()` при APNs `410 Unregistered`/`400 BadDeviceToken` или явной отписке `DELETE /v1/devices/{token}`. Выборка для push игнорирует `invalidated_at IS NOT NULL`. |

> APNs credentials (`.p8`-ключ, `APNS_*`) — **внешняя зависимость пользователя** (Apple Developer), не в БД; хранятся как секрет/конфиг-артефакт ([07-deployment.md](07-deployment.md), [ADR-013](adr/ADR-013-apns-push-from-job-events.md)).

### billing_events (Adapty webhook ledger)

| Поле | Тип | Заметки |
|---|---|---|
| `id` | bigserial PK | |
| `adapty_event_id` | text UNIQUE NOT NULL | **Идемпотентность** обработки вебхуков (`= webhook.event_id`). Единственная точка дедупа; повтор → `200` no-op. |
| `event_type` | text | Adapty webhook v2: `subscription_started` / `subscription_renewed` / `subscription_expired` / `subscription_refunded` / `billing_issue_detected` / `access_level_updated`. Маппинг → `subscriptions` — нормативно [modules/billing/03-architecture.md §2.3](modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица). |
| `user_id` | text FK→users NULL | После маппинга. |
| `payload` | jsonb | Сырой вебхук. |
| `processed_at` | timestamptz NULL | NULL = принят, не обработан. |
| `received_at` | timestamptz | |
