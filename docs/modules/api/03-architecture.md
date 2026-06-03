# api — Architecture

## Слои (`app/api`)
- **Routers** — endpoints по доменам: `projects`, `jobs`, `billing`.
- **Dependencies** — auth (Bearer → user), quota-gate, idempotency.
- **Schemas** (`app/schemas`) — Pydantic-модели запросов/ответов (контракт iOS).
- **Services** (`app/services`) — `project`/`job`/`usage`: бизнес-операции, постановка задач в очередь.
- API не импортирует Anthropic SDK и Docker — только Postgres-сессии и Celery `.delay()`/Redis.

## Middleware / dependencies
- **Auth:** `Authorization: Bearer <key>` → `current_user`. **Sprint 3:** индексируемый O(1) lookup по `key_id` + один constant-time argon2-verify — нормативный контракт [modules/auth/03-architecture.md](../auth/03-architecture.md) ([ADR-008](../../adr/ADR-008-indexed-api-key-lookup.md), закрывает [TD-004](../../100-known-tech-debt.md#td-004)). Нет/невалиден/отозван → `401`. Первичный логин (Sign in with Apple) — `POST /auth/apple` ([ADR-007](../../adr/ADR-007-sign-in-with-apple.md)). Rate-limit 60/min на ключ → `429`.
- **Quota-gate** (на `POST /projects`, `/edits`): проверка активного access level (модуль `billing`) + остатка `usage_counters` vs `plan_quotas` + cap конкурентных джоб ([modules/auth/03-architecture.md §6](../auth/03-architecture.md)). Нет прав → `402` (RFC-7807 с required entitlement). Политика сверки — вебхуки + ресинк ([Q-BILLING-2](../../99-open-questions.md#q-billing-2) resolved). В S3 access_level — дефолт free до подключения billing (S3.5).
- **Idempotency** (на `POST /projects`, `/edits`): `Idempotency-Key` header → дедуп по партиальному UNIQUE `(user_id, idempotency_key)` в `generation_jobs` (опирается на денормализованный `generation_jobs.user_id`, см. [03-data-model.md](../../03-data-model.md#generation_jobs)). Повтор → тот же `202` с тем же `job_id`.
- **Webhook auth** (`POST /billing/webhook/adapty`): верификация секрета Adapty, **не** Bearer.

## Async-инвариант
- Любая тяжёлая операция возвращает `202` + `job_id` и ставит задачу в Celery. API не блокируется на Claude/сборке.

## Статус джобы
- **Канонический:** `GET /jobs/{id}` (polling) — читает `generation_jobs.state` + связанные.
- **Live:** `GET /jobs/{id}/events` (SSE) — подписка на Redis pub/sub `job:{id}`. Воркеры публикуют события из `job_events`.

## Конвенции
- Префиксные opaque ID: `p_`, `j_`, `r_`, `d_`.
- `202` на всё асинхронное.
- Ошибки — RFC-7807 (`application/problem+json`).
- Версионирование пути: `/v1`.
- Авторизация владения на каждом `/{pid}`/`/{jid}` (cross-tenant защита).

## Обработчики ошибок → RFC-7807 (нормативно, ВСЕ ошибки включая 422)

> **Прод-фикс (2026-06-04).** Валидационный `422` на `POST /auth/apple` отдавался **дефолтным FastAPI** `{detail:[...]}` (`application/json`) вместо `application/problem+json`. Контракт [api/02 → Конвенции ошибок](02-api-contracts.md#конвенции-ошибок-rfc-7807) и [B.3](02-api-contracts.md#b3-состав-описания-каждого-endpointа-обязательный-минимум) требуют RFC-7807 для **ВСЕХ** ошибок, **включая 422**. Прочие 422 (`/devices`, отсутствие `Idempotency-Key`) уже нормализованы — `/auth/apple` выпадал.

**Нормативный контракт (единая точка нормализации, для всех роутеров включая `/auth/apple`):**
- Регистрируется **глобальный** `exception_handler(RequestValidationError, ...)` (Starlette/FastAPI app-level), который сериализует ошибку валидации в `application/problem+json` (RFC-7807: `type`/`title`/`status=422`/`detail`; для валидации — `detail` агрегирует поля или ссылается на `errors[]`). Тот же app-level хэндлер покрывает `HTTPException`/доменные ошибки (`401`/`402`/`404`/`409`/`429`) → `problem+json`.
- Хэндлер **app-level** (на `FastAPI(...)`-инстансе), поэтому распространяется на **ВСЕ** эндпоинты, включая публичный-без-Bearer `POST /auth/apple` ([auth/02 §Ошибки](../auth/02-api-contracts.md#post-authapple): `422` при отсутствии `identity_token`). Отдельный per-router обработчик не нужен — единая точка исключает «забытые» эндпоинты.
- **Критерий приёмки (qa):** `POST /auth/apple` без `identity_token` → `422` с `Content-Type: application/problem+json` и телом RFC-7807 (не дефолтный `{detail:[...]}`); grep-проверка, что **все** 422 в API несут `application/problem+json` ([06-testing-strategy.md](../../06-testing-strategy.md)).
