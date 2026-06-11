# api — API Contracts (iOS)

Base: `https://api.domain/v1` · Auth: `Authorization: Bearer <api-key>` (кроме вебхука Adapty и `POST /auth/apple`) · Ошибки: RFC-7807 (`application/problem+json`).

> **Auth-endpoints** (`/auth/apple`, `/auth/tokens`) — детальный контракт в [modules/auth/02-api-contracts.md](../auth/02-api-contracts.md) (Sprint 3). Формат Bearer-ключа `lv_<key_id>_<secret>` ([ADR-008](../../adr/ADR-008-indexed-api-key-lookup.md)). Rate-limit 60 req/min на ключ → `429`.

## Сводка endpoints

| Method | Path | Назначение | Auth | Success |
|---|---|---|---|---|
| POST | `/auth/apple` | Sign in with Apple → наш Bearer-ключ (S3) | Apple token | `200` |
| GET | `/auth/tokens` | список токенов/устройств (S3) | Bearer | `200` |
| DELETE | `/auth/tokens/{id}` | отозвать токен / logout (S3) | Bearer | `204` |
| POST | `/projects` | создать проект + старт генерации | Bearer | `202` |
| GET | `/projects` | список проектов пользователя | Bearer | `200` |
| GET | `/projects/{pid}` | детали проекта + live URL | Bearer | `200` |
| DELETE | `/projects/{pid}` | удалить проект + полный GC ресурсов (S4) | Bearer | `202` |
| POST | `/projects/{pid}/edits` | post-delivery правка → Agent 4 (S5) | Bearer | `202` |
| GET | `/projects/{pid}/revisions` | история ревизий | Bearer | `200` |
| POST | `/projects/{pid}/revisions/{revision_no}/rollback` | откат на good-ревизию (S5) | Bearer | `202` |
| POST | `/devices` | регистрация APNs device token (S5) | Bearer | `201` |
| DELETE | `/devices/{apns_token}` | отписка устройства (S5) | Bearer | `204` |
| GET | `/jobs/{jid}` | poll статуса (канонический) | Bearer | `200` |
| GET | `/jobs/{jid}/events` | SSE live-статус (reconnect/Last-Event-ID, S5) | Bearer | `200` (event-stream) |
| GET | `/jobs/{jid}/questions` | уточняющие вопросы | Bearer | `200` |
| POST | `/jobs/{jid}/answers` | ответы → резюм пайплайна (→ SPECCING) | Bearer | `202` |
| GET | `/billing/me` | тариф/entitlement + остаток квоты | Bearer | `200` |
| POST | `/billing/webhook/adapty` | приём вебхуков Adapty (S2S) | **Adapty secret** | `200` |

## POST /projects
Создаёт проект и стартует генерацию (Agent 1).
- Headers: `Idempotency-Key` (обяз.).
- Body: `{ "prompt": "string", "title": "string?" }`.
- **Язык контента сайта НЕ принимается параметром** — детерминированный серверный детект из **исходного** `prompt` (язык сайта = язык исходного промпта пользователя), [ADR-028](../../adr/ADR-028-deterministic-source-prompt-language-detection.md) ревизует [ADR-025](../../adr/ADR-025-content-language-autodetect-spec-marker.md), [pipeline §Язык/локализация](../pipeline/03-architecture.md#языклокализация-контента-сайта--детерминированный-детект-adr-028-ревизует-adr-025). Схема body не меняется; явный `locale`-override — вне MVP ([Q-LOCALE-1](../../99-open-questions.md#q-locale-1)).
- Гейтинг: quota-gate (активный entitlement + остаток квоты — генерации/`max_projects`/`max_concurrent`). Нарушение → `402`. Контракт гейта — [modules/billing/02-api-contracts.md §3](../billing/02-api-contracts.md#3-quota-gate-на-post-v1projects-и-post-v1projectspidedits).
- `202` → `{ "project_id": "p_...", "job_id": "j_..." }`.
- Ошибки: `401`, `402` (RFC-7807, поля `required_entitlement` + `reason` ∈ `no_entitlement`/`quota_exhausted`/`project_limit`/`concurrency_limit`), `422`.

## GET /projects · GET /projects/{pid}
- `200` список / детали: `{ "id", "title", "prompt", "current_revision_id", "live_url": "string?", "created_at" }`.
- `404` если не принадлежит пользователю (cross-tenant — не раскрываем существование).
- **Листинг/детали исключают soft-deleted проекты** (`deleted_at IS NOT NULL` — фильтр `deleted_at IS NULL`, [ADR-011](../../adr/ADR-011-project-delete-gc.md)): удаляемый/удалённый проект → `404`.

## DELETE /projects/{pid} (Sprint 4)
Удаляет проект и запускает полный GC всех его ресурсов. Нормативный контракт — [ADR-011](../../adr/ADR-011-project-delete-gc.md); исполнение GC (teardown контейнеров/route, volume, S3-артефакты всех ревизий, БД-каскад) — [modules/deploy/03-architecture.md §6](../deploy/03-architecture.md#6-gc-при-удалении-проекта-sprint-4--delete-projectsid-adr-011-закрывает-td-003q-deploy-3).
- Auth: Bearer; владение проверяется — чужой/несуществующий `pid` → `404` (cross-tenant, не раскрываем существование).
- **`202 Accepted`** → `{ "project_id": "p_...", "status": "deleting" }`. GC асинхронный (Celery `project.gc`, `queue=build`); проект **сразу** soft-delete (`projects.deleted_at=now()`) и исчезает из `GET /projects`.
- **In-flight джобы** проекта переводятся в `FAILED(project_deleted)` (снимаются из `active_jobs`/concurrency-cap); диспетчер не ставит новых витков по soft-deleted проекту ([ADR-011 §C](../../adr/ADR-011-project-delete-gc.md)).
- **Идемпотентность:** повторный `DELETE` уже удаляемого проекта → `202` (no-op путь) либо `404`, если строка уже физически удалена GC. Безопасно повторяем.
- subdomain'ы проекта **не реюзаются** (opaque, защита от subdomain-takeover).
- Ошибки: `401`, `404`.

## GET /jobs/{jid}
Канонический статус.
- `200` → `{ "id", "project_id", "state", "retry_count", "failure_reason": "string?", "live_url": "string?", "updated_at" }`.
- `state` ∈ `CREATED, INTERVIEWING, AWAITING_CLARIFICATION, SPECCING, BUILDING, DEPLOYING, LIVE, FIXING, FAILED`.
- **Cross-tenant:** чужой/несуществующий `jid` → `404` (фильтр по `user_id`, не раскрываем существование). Этот же инвариант наследует SSE `GET /jobs/{jid}/events`.

## GET /jobs/{jid}/events (SSE) — полный контракт Sprint 5 ([ADR-012](../../adr/ADR-012-sse-realtime-transport.md))

> **Статус S1→S5:** в S1 endpoint объявлен как минимальный стрим из Redis pub/sub без нормативной семантики reconnect/heartbeat/завершения. **Sprint 5** разворачивает полный исполняемый контракт (replay из `job_events`, `Last-Event-ID`, heartbeat, завершение на терминале, cross-tenant). Polling `GET /jobs/{jid}` остаётся равноправным fallback (уже есть).

- **Content-Type:** `text/event-stream`. Источник live — Redis pub/sub `job:{jid}`; источник истины replay — `job_events` (Postgres, append-only).
- **Auth + cross-tenant:** Bearer; владение (`generation_jobs.user_id == auth.user_id`) → иначе `404` (как `GET /jobs/{jid}`, не раскрываем существование чужой джобы). Невалидный/нет Bearer → `401`.
- **Event-id = `job_events.id`** (bigserial, монотонный per-job). Каждый кадр несёт `id: {job_events.id}`.
- **Формат кадра:**
  ```
  id: 1287
  event: state_changed
  data: {"event_type":"state_changed","from_state":"BUILDING","to_state":"DEPLOYING","payload":{...},"created_at":"..."}

  ```
  `event:` = `job_events.event_type`; `data:` — JSON (`{ "event_type", "from_state": "string?", "to_state": "string?", "payload": {...}, "created_at" }`).
- **Reconnect / Last-Event-ID:** клиент при переподключении шлёт заголовок `Last-Event-ID: <n>` (или query `?last_event_id=<n>`). Сервер: **сначала подписка на Redis `job:{jid}`, затем catch-up из `job_events WHERE job_id=:jid AND id > :n ORDER BY id`, дедуп live-кадров с `id <=` последнего отданного из БД** (порядок защищает от потери событий в окне между catch-up и подпиской — [ADR-012](../../adr/ADR-012-sse-realtime-transport.md)). Без `Last-Event-ID` — первый кадр = текущий снимок (последнее `state_changed`), далее live.
- **Heartbeat:** каждые `SSE_HEARTBEAT_S` (env, default 15 s) — SSE-комментарий `: ping` (keepalive, не событие; клиент игнорирует). Держит idle-соединение (`AWAITING_CLARIFICATION` до 7 дней — ноль событий) через прокси/NAT.
- **Reconnect-hint:** первый кадр несёт `retry: {SSE_RETRY_MS}` (env, default 3000).
- **Завершение:** на терминальном `state` (`LIVE`/`FAILED`) — финальное событие + кадр `event: done` → сервер закрывает стрим; клиент по `done` **не** переподключается. Если джоба уже терминальна при подключении — снимок + `done` + закрытие (не держим вечное соединение).
- **Лимит соединений:** установление стрима считается запросом (rate-limit 60/min на ключ); сверх `SSE_MAX_STREAMS_PER_KEY` (env, default 5) одновременных стримов на ключ → `429`.
- **Fallback:** при недоступности SSE — polling `GET /jobs/{jid}` (равноправный путь, [08 §5-4](../../08-product-decisions.md#sprint-5--realtime--edits)).

## POST /devices · DELETE /devices/{apns_token} (Sprint 5, APNs)

Регистрация/отписка устройства для APNs push ([ADR-013](../../adr/ADR-013-apns-push-from-job-events.md)). Auth: Bearer.

**POST /devices**
- Body: `{ "apns_token": "string", "platform": "ios", "environment": "sandbox|production" }`.
- Upsert по `(user_id, apns_token)` в `device_tokens` (повторная регистрация того же токена — идемпотентно, сбрасывает `invalidated_at`).
- `201` → `{ "id": "dev_..." }`. Ошибки: `401`, `422` (невалидный токен/`environment`).

**DELETE /devices/{apns_token}**
- Отписка (logout/смена устройства) — `device_tokens.invalidated_at = now()` по `(user_id, apns_token)`.
- `204`. Чужой/несуществующий токен → `404` (cross-tenant — выборка по `user_id`). Идемпотентно (повтор → `204`/`404`).

> Push отправляется асинхронно (`notify.apns_push`) при переходах `LIVE`/`FAILED`/`AWAITING_CLARIFICATION` — нормативный перечень и механика в [ADR-013](../../adr/ADR-013-apns-push-from-job-events.md), модуль [notify](../notify/README.md).

## GET /jobs/{jid}/questions
- `200` → `{ "questions": [ { "id", "position", "text", "kind": "choice|free_text?", "options": [..]? } ] }`.
- Доступно в `AWAITING_CLARIFICATION`.

## POST /jobs/{jid}/answers
Резюм пайплайна. Детерминированное поведение — [Q-PIPELINE-2](../../99-open-questions.md#q-pipeline-2) (closed-for-S1).
- Body: `{ "answers": [ { "question_id": "string", "text": "string" } ] }`.
- Все ответы применяются и джоба переводится `AWAITING_CLARIFICATION → SPECCING` (ставится task `SPECCING`).
- **Валидация полноты:** должны быть отвечены все обязательные вопросы джобы; `question_id` обязан принадлежать этой джобе.

### Матрица состояний и ответов

| Условие | Результат | Код |
|---|---|---|
| Первый валидный сабмит в `AWAITING_CLARIFICATION` | применить ответы, перейти в `SPECCING`, поставить task | `202` `{ "job_id" }` |
| Повторный сабмит **тех же** ответов (тот же набор `question_id`+`text`), джоба уже в `SPECCING`+ | **идемпотентно**: ничего не меняем, возвращаем тот же `job_id` | `200` `{ "job_id" }` |
| Сабмит, когда джоба уже продвинулась (`SPECCING`/`BUILDING`/`DEPLOYING`/`LIVE`/`FIXING`) с **другими** ответами | конфликт — ответы уже зафиксированы и пайплайн идёт | `409` (RFC-7807, `type=.../conflict`) |
| Сабмит в терминальном `FAILED` | джоба не возобновляема ответами | `409` |
| Частичные ответы (не на все обязательные вопросы) или конфликтующие/дублирующиеся `question_id` в одном теле, или `question_id` чужой джобы | невалидный payload | `422` |

- **Идемпотентность определяется** сравнением нормализованного набора `(question_id, text)` с уже сохранёнными `answers` джобы: совпал → `200` (idempotent replay); не совпал и состояние ≠ `AWAITING_CLARIFICATION` → `409`.
- `409`/`422` — `application/problem+json`; `409.detail` указывает текущий `state`.

## POST /projects/{pid}/edits (Sprint 5)
Post-delivery правка (Agent 4 как editor, цикл `LIVE → FIXING → LIVE`, новый Revision). Контракт цикла — [modules/pipeline/03-architecture.md → post-delivery edit](../pipeline/03-architecture.md#post-delivery-edit-live--fixing--live--контракт-зафиксирован-реализация-в-sprint-5).
- Headers: `Idempotency-Key` (обяз.) — дедуп `(user_id, idempotency_key)` (`generation_jobs`).
- Body: `{ "instruction": "string" }`. Текст `instruction` сохраняется в append-only `job_events` (`event_type='edit_requested'`, `payload.instruction`) — **отдельной колонки `generation_jobs.instruction` нет** ([03-data-model.md → generation_jobs](../../03-data-model.md#generation_jobs)).
- **Гейтинг (отдельный лимит правок, [ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)):** та же dependency `quota_gate`, но при `kind=edit` сверяет `edit_usage_counters.edits_used < plan_quotas.monthly_edits` (**не** `monthly_generations`), плюс `access_level` активен и `max_concurrent_jobs` (edit-джоба считается активной). `max_projects` не проверяется. Контракт — [modules/billing/03-architecture.md §7](../billing/03-architecture.md#7-граница-s5-edits).
- `202` → `{ "job_id": "j_..." }`. Ошибки: `402` (RFC-7807, `reason ∈ {no_entitlement, edit_quota_exhausted, concurrency_limit}` + `required_entitlement`), `409` (проект не `LIVE` — правка возможна только над `LIVE`-сайтом), `404` (чужой/несуществующий `pid`).

## GET /projects/{pid}/revisions
- `200` → `{ "current_revision_id": "r_...", "revisions": [ { "id", "revision_no", "is_good", "created_from_job_id", "created_at" } ] }`.
- `current_revision_id` — активная good-ревизия (= `projects.current_revision_id`), чтобы UI отметил текущую для rollback. `404` если проект не принадлежит пользователю.

## POST /projects/{pid}/revisions/{revision_no}/rollback (Sprint 5)
Откат на ранее задеплоенную good-ревизию ([ADR-014 §B](../../adr/ADR-014-edit-limit-revision-rollback.md), [08 §5-3](../../08-product-decisions.md#sprint-5--realtime--edits)). Передеплой существующей ревизии без новой генерации/правки — **лимитом правок/генераций не гейтится**.
- Auth: Bearer; владение → `404` (cross-tenant).
- Целевая ревизия обязана быть `is_good=true` и принадлежать проекту: иначе `409` (`type=.../conflict`, не good / уже текущая) или `404` (нет такой `revision_no` у проекта).
- **`202 Accepted`** → `{ "job_id": "j_...", "target_revision_no": <n> }`. Порождает джобу **`generation_jobs.kind='rollback'`** (прямой re-deploy good-ревизии, **минуя `FIXING`** — без Agent 4/fix-loop; [03-data-model.md → generation_jobs.kind](../../03-data-model.md#generation_jobs)). Re-deploy асинхронный (Celery `queue=build`): новый `site_deployments` целевой ревизии → health `200` → прежний `active`-деплой `→ superseded` (teardown), `projects.current_revision_id` ← целевая. Health-fail нового деплоя оставляет прежнюю ревизию активной (без downtime), джоба → `FAILED(infra_error)` — [modules/deploy/03-architecture.md §7](../deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014).
- **Идемпотентность:** rollback на ревизию, которая уже `current_revision_id` → `409` (нечего откатывать) или no-op `202` с тем же результатом; повторный rollback с тем же `Idempotency-Key` (если задан) не плодит деплои.
- Прогресс re-deploy наблюдаем через `GET /jobs/{jid}` / SSE по возвращённому `job_id`. Ошибки: `401`, `404`, `409`.

## GET /billing/me
- `200` → `{ "access_level", "status", "period": "YYYY-MM", "quota": { "monthly_generations", "generations_used", "generations_remaining", "monthly_edits", "edits_used", "edits_remaining", "max_concurrent_jobs", "active_jobs", "max_projects", "projects_used" } }` (поля правок — S5, [ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)).
- Полная схема + источники — [modules/billing/02-api-contracts.md §2](../billing/02-api-contracts.md#2-get-v1billingme) (нормативный источник). Источник: кэш `subscriptions` (lazy-ресинк при протухшем `synced_at`) + `usage_counters`/`edit_usage_counters`/`plan_quotas`.

## POST /billing/webhook/adapty
- Server-to-server, **не** Bearer. Верификация секрета/подписи Adapty.
- Идемпотентно по `adapty_event_id`.
- Детали — [modules/billing/02-api-contracts.md](../billing/02-api-contracts.md).
- `200` всегда при успешном приёме (даже на дубль — идемпотентно). Невалидная подпись → `401`.

## Конвенции ошибок (RFC-7807)
```json
{ "type": "https://api.domain/errors/payment-required",
  "title": "Payment Required",
  "status": 402,
  "detail": "Active subscription required to generate.",
  "required_entitlement": "pro" }
```

---

# Публичная API-документация (Swagger/OpenAPI) — нормативный стандарт

> **Назначение.** OpenAPI-схема (`/openapi.json`) и Swagger UI (`/docs`) — **публичный справочник для внешнего iOS-разработчика**, а не внутренняя инженерная документация. Этот раздел — нормативный стандарт, которому backend **обязан** следовать при формировании FastAPI-метаданных, `summary`/`description`/`tags` эндпоинтов и docstring/`description` Pydantic-схем. Источник истины контрактов остаётся выше в этом файле (+ auth/billing `02-api-contracts.md`); данный раздел регламентирует **как они подаются наружу**.

## B.1 Язык и аудитория
- Все публичные тексты (`title`/`description` приложения, `summary`/`description` эндпоинтов, `tags`-описания, описания полей схем, описания ответов) — **на русском языке**. Технические идентификаторы (имена путей, query/header-ключей, enum-значений, JSON-полей, HTTP-кодов) — в оригинале.
- Тон — справочный, для интегратора со стороны iOS-приложения, **не знающего** внутреннего устройства бэкенда. Описание объясняет **назначение и поведение endpoint'а с точки зрения клиента**, а не реализацию.

## B.2 ЗАПРЕЩЁННЫЕ подстроки в публичной схеме (нормативный denylist)
В `summary`/`description`/docstring/`openapi.json` (всё, что попадает в публичную схему) **ЗАПРЕЩЕНЫ**:
- маркеры процесса: **«Sprint N»** / «Спринт N», **«ADR-NNN»**, **«TD-NNN»**, «Q-NNN-N»;
- имена внутренних агентов: **Interviewer / Spec writer / Builder / Fixer**, «Agent 1..4», «Agent N»;
- имена внутренних модулей/подсистем (`pipeline`, `deploy`, `billing`-internal, `notify`, `observability`, `reconciler`, `sweeper`, `dispatcher`), reviewer'ов, термины оркестрации/state-machine как внутренней механики;
- ссылки на внутренние файлы docs, имена внутренних таблиц/полей БД, имена Celery-очередей/задач.

> Поведение, которое клиенту **нужно** знать (например, что генерация асинхронная и статус опрашивается через `GET /jobs/{jid}` или SSE; что бывает фаза уточняющих вопросов), описывается **в продуктовых терминах** («сервис задаёт уточняющие вопросы», «генерация выполняется асинхронно»), **без** внутренних имён агентов/состояний-как-механики. Перечень публично-видимых значений `state` (enum `GET /jobs/{jid}`) допустим — это часть клиентского контракта, но **без** описания внутренней семантики переходов.

## B.3 Состав описания каждого endpoint'а (обязательный минимум)
Для каждого публичного endpoint'а OpenAPI обязан нести:
- **метод и путь** (FastAPI выводит автоматически);
- **`summary`** (рус., краткая) и **`description`** (рус., назначение с точки зрения клиента);
- **параметры**: path/query/header — с описаниями. Обязательно документировать:
  - `Authorization: Bearer <api-key>` (формат ключа `lv_<key_id>_<secret>`) — на всех Bearer-эндпоинтах;
  - `Idempotency-Key` — на `POST /projects` и `POST /projects/{pid}/edits` (обяз.);
  - `Last-Event-ID` (header) / `last_event_id` (query) — на SSE `GET /jobs/{jid}/events`;
- **тело запроса** — схема с описанием смысла ключевых полей (рус.);
- **схемы ответов по кодам**, включая ошибки: success-код + релевантные `401`/`402`/`404`/`409`/`422`/`429`. Ошибки — модель **RFC-7807** (`application/problem+json`, поля `type`/`title`/`status`/`detail` + доменные `required_entitlement`/`reason` где применимо);
- **смысл ключевых полей** ответа (рус.) — что значит `state`, `live_url`, `quota.*` и т.п.

## B.4 Группировка по доменам — `tags` (нормативный перечень, русские названия)
Эндпоинты группируются `tags` с **русскими** названиями и описаниями. Нормативный маппинг endpoint → tag (имена тегов — публичные, доменные, **без** внутренних имён модулей):

| Tag (рус.) | Назначение (description, рус.) | Эндпоинты |
|---|---|---|
| **Аутентификация** | Вход через Apple, регистрация/вход по `user_id`+секрет, управление токенами устройств | `POST /auth/apple`, `POST /auth/register`, `POST /auth/login`, `POST /auth/secret`, `GET /auth/tokens`, `DELETE /auth/tokens/{id}` |
| **Проекты** | Создание сайтов, список, детали, удаление | `POST /projects`, `GET /projects`, `GET /projects/{pid}`, `DELETE /projects/{pid}` |
| **Джобы генерации** | Статус генерации, уточняющие вопросы, ответы, live-поток событий | `GET /jobs/{jid}`, `GET /jobs/{jid}/events`, `GET /jobs/{jid}/questions`, `POST /jobs/{jid}/answers` |
| **Правки и ревизии** | Post-delivery правки сайта, история ревизий, откат | `POST /projects/{pid}/edits`, `GET /projects/{pid}/revisions`, `POST /projects/{pid}/revisions/{revision_no}/rollback` |
| **Устройства** | Регистрация/отписка устройств для push-уведомлений | `POST /devices`, `DELETE /devices/{apns_token}` |
| **Биллинг** | Тариф, остаток квоты, приём событий магазина подписок | `GET /billing/me`, `POST /billing/webhook/adapty` |

> Имена tag'ов и состав — единственный нормативный источник; backend проставляет `tags=[...]` ровно по этой таблице. (Внутренняя модульная принадлежность — `api`/`auth`/`billing` — наружу **не** выносится; группировка чисто доменная.)

## B.5 Скрытие служебных / internal эндпоинтов из публичной схемы
- **`include_in_schema=False`** (скрыть из `/openapi.json` и `/docs`) для **внутренних** эндпоинтов, не предназначенных iOS-клиенту:
  - **`GET /metrics`** (Prometheus, internal-only — [observability §1](../observability/03-architecture.md#1-экспозиция-metrics));
  - **`GET /healthz` / `GET /readyz`** (liveness/readiness, инфра — [07-deployment.md → Health/readiness](../../07-deployment.md#health--readiness)).
- **`POST /billing/webhook/adapty`** — **оставить** в схеме, но с явной пометкой в `description`: **server-to-server (S2S)**, вызывается провайдером подписок, **не** требует Bearer (верификация по секрету провайдера). Тег — «Биллинг». Пометка нужна, чтобы iOS-разработчик понимал, что этот endpoint **не** для клиента.
- **`POST /auth/register` · `POST /auth/login`** ([ADR-024](../../adr/ADR-024-user-id-secret-authentication.md)) — **публичные** (вход/регистрация, **не** требуют Bearer — как `/auth/apple`), `include_in_schema=True`, тег «Аутентификация». Глобальный `BearerAuth` ([B.6 securityScheme](#b6-глобальные-метаданные-fastapi-рус-без-внутренних-маркеров)) на них **не** обязателен — описать в `description`, что авторизация не требуется (как у `/auth/apple`); лишний `Authorization`-заголовок безвреден. `POST /auth/secret` — наоборот, **требует** Bearer (глобальный `BearerAuth` применяется).
- Прочие эндпоинты из сводной таблицы выше — публичные (`include_in_schema=True`, дефолт).

## B.6 Глобальные метаданные FastAPI (рус., без внутренних маркеров)
Конструктор `FastAPI(...)` задаёт публичные метаданные:
- **`title`** — публичное имя API (рус.), напр. «Corely API» / «API генерации сайтов» (без «Lovable-AI internal», без кодовых имён);
- **`description`** — краткая вводная для **iOS-разработчика** (рус.): что делает API (генерация сайтов по текстовому промту), как авторизоваться (`Authorization: Bearer`, ключ из `POST /auth/apple`), что генерация асинхронная (статус — `GET /jobs/{jid}` или SSE), формат ошибок (RFC-7807). **Без** «Sprint»/«ADR»/имён агентов/модулей;
- **`version`** — публичная версия API (semver, напр. `1.0.0`), **без** внутренних маркеров;
- `servers` — публичный base `https://corelysite.shop/v1` (prod).

## B.7 Чек-лист для reviewer/qa (grep-критерии чистоты `/openapi.json`)
Сериализованная публичная схема (`/openapi.json`) обязана **не содержать** внутренних маркеров. Нормативные grep-критерии (case-insensitive по сериализованному JSON; ни одна подстрока не должна встречаться):

| Подстрока (запрещена) | Что отлавливает |
|---|---|
| `Sprint` / `Спринт` | маркеры спринтов |
| `ADR-` | ссылки на ADR |
| `TD-` | tech-debt маркеры |
| `Q-` (в значениях описаний) | open-question маркеры |
| `Interviewer`, `Builder`, `Fixer`, `Spec writer`, `Agent 1`/`Agent 2`/`Agent 3`/`Agent 4` | имена внутренних агентов |
| `reconciler`, `sweeper`, `dispatcher` | внутренние термины оркестрации |

Проверка — автоматизируемая (qa: тест, который сериализует `app.openapi()` и ассертит отсутствие подстрок; включается в [06-testing-strategy.md](../../06-testing-strategy.md)). Наличие любой подстроки = **major**-замечание (функциональный пробел публичного контракта), не minor.

> **Граница.** Этот стандарт регламентирует **публичную подачу**. Внутренняя инженерная документация (этот файл, ADR, модульные docs) остаётся на месте и продолжает использовать Sprint/ADR/имена агентов — она просто **не утекает** в `/openapi.json`. Docstring'и FastAPI-роутов, попадающие в `description`, — публичны → подчиняются denylist B.2; внутренние комментарии в коде (`# ...`) в схему не попадают и под denylist не подпадают.
