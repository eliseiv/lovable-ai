# billing — Overview

Статус: **реализован (Sprint 3.5)** (развёрнут из [08-product-decisions §3.5](../../08-product-decisions.md#sprint-35--billing-adapty), Q-BILLING-1/2/3 resolved; qa 417 passed / coverage 88%, reviewer `production_ready: true`). Продуктовые решения зафиксированы и не пересматриваются — здесь разворачивается *как* они исполняются. Живой E2E с реальным Adapty + Claude+Docker — открытый приёмочный пункт ([Q-BILLING-4](../../99-open-questions.md#q-billing-4)); резолюция signature-header/payload v2 — при реальной интеграции.

## Scope (Sprint 3.5)
- **Webhook handler** (`app/billing/webhook_handler`) — `POST /v1/billing/webhook/adapty`: server-to-server, верификация секрета/подписи Adapty (**НЕ** Bearer), **идемпотентность** по `adapty_event_id` (ledger `billing_events`, UNIQUE), маппинг событий Adapty → апдейт `subscriptions`. Контракт — [02-api-contracts.md §1](02-api-contracts.md#1-post-v1billingwebhookadapty), маппинг событий — [03-architecture.md §2](03-architecture.md#2-webhook-handler-post-v1billingwebhookadapty).
- **adapty_client** (`app/billing/adapty_client`) — async httpx-клиент к Adapty Server-side API v2 (`getProfile`/validate) для ресинка; rate-limit к Adapty. Зависимость объявлена в [02-tech-stack §Биллинг](../../02-tech-stack.md#биллинг).
- **resync** (`app/billing/resync`) — Celery-beat job `billing.resync` (интервал `BILLING_RESYNC_INTERVAL_S`): сверка `subscriptions.access_level` через `getProfile` (страховка от пропущенных вебхуков), идемпотентно. Плюс **lazy-ресинк по требованию** при гейте, если кэш протух. [03-architecture.md §3](03-architecture.md#3-ресинк-getprofile).
- **entitlements** (`app/billing/entitlements`) — `resolve_access_level(user_id)` / `resolve_max_concurrent_jobs(user_id)` — реальный источник `access_level` (кэш `subscriptions`), **заменяет S3-заглушку free** в модуле `auth`. [03-architecture.md §4](03-architecture.md#4-entitlements--quota-gate).
- **quota_gate** (`app/billing/quota_gate`) — dependency для middleware `api` на `POST /v1/projects` и `POST /v1/projects/{pid}/edits`: активный entitlement + остаток квоты (`generations`/`max_projects`/`max_concurrent`) → `402` (RFC-7807) при отсутствии/исчерпании. [03-architecture.md §4](03-architecture.md#4-entitlements--quota-gate).
- **GET /v1/billing/me** — текущий entitlement + остаток квоты. [02-api-contracts.md §3](02-api-contracts.md#3-get-v1billingme).
- **Сидинг `plan_quotas`** (Free + Pro) — Alembic data-migration; **инкремент `usage_counters`** на **успешный старт генерации** (не на `/answers`). [03-architecture.md §5](03-architecture.md#5-учёт-usage_counters).
- **Grace-период сайтов** (`app/billing/subscription_sweeper`) — Celery-beat job `billing.subscription_sweep` (интервал `SUBSCRIPTION_SWEEP_INTERVAL_S`): при `subscriptions.status=grace` и `grace_until < now()` — teardown сайтов пользователя (переиспользует deploy-механику). [03-architecture.md §6](03-architecture.md#6-grace-период-сайтов-q-billing-1).

## Out-of-scope
- Покупка/пейволы — на стороне iOS (Adapty SDK).
- Собственный процессинг чеков/IAP — не делаем ([ADR-004](../../adr/ADR-004-adapty-source-of-truth.md)).
- HTTP-роутинг и подключение dependency к роутам (`POST /projects`, `/edits`) — модуль `api`. `billing` отдаёт переиспользуемую FastAPI-dependency (`quota_gate`) и функции `entitlements`.
- **Граница S5:** `POST /projects/{pid}/edits` реализуется в Sprint 5. Контракт quota-gate на `/edits` зафиксирован здесь **сейчас** и активируется, когда `/edits` появится. В S3.5 quota-gate **реально энфорсится только на `POST /projects`**; на `/edits` — контракт готов, dispatch-точка та же ([03-architecture.md §7](03-architecture.md#7-граница-s5-edits)).

## Зависимости
- Adapty Server-side API v2 (`getProfile`, webhook), Postgres (`subscriptions`/`plan_quotas`/`usage_counters`/`billing_events`/`users`), Redis (rate-limit к Adapty, кэш-флаг свежести), Celery beat.
- Модуль `deploy` — teardown сайтов при истечении grace ([deploy §5](../deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)).
- Модуль `auth` — `quota_gate`/`entitlements` подключаются туда, где S3 ставил заглушку free ([auth §6](../auth/03-architecture.md)).

## Решения (resolved / ADR)
- Тарифы/квоты — Free+Pro freemium ([Q-BILLING-1](../../99-open-questions.md#q-billing-1), [08 §3.5](../../08-product-decisions.md#sprint-35--billing-adapty)). Нормативный источник чисел — `plan_quotas` ([03-data-model](../../03-data-model.md#plan_quotas)) + таблица [08 §3.5](../../08-product-decisions.md#sprint-35--billing-adapty); single normative source.
- Dual-source прав (вебхуки источник истины + `getProfile`-ресинк, идемпотентность вебхука по `adapty_event_id`) — [ADR-004](../../adr/ADR-004-adapty-source-of-truth.md) + **[ADR-009](../../adr/ADR-009-billing-idempotency-resync-grace.md)** (исполняемая модель идемпотентности/ресинка/grace-teardown через beat).
- Маппинг `customer_user_id = user.id` ([Q-BILLING-3](../../99-open-questions.md#q-billing-3)).
- Реальные Adapty product IDs — **внешняя зависимость** (дашборд Adapty), привязываются позже.
