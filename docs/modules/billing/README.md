# Модуль `billing`

**Статус:** **реализован (Sprint 3.5)**; **ревизия приёма вебхука [ADR-027](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md) зафиксирована в docs — требует доработки кода** (Bearer вместо HMAC, always-200-on-bad-input, token-grant по тиру, `subscription_cancelled`). · **Владелец кода:** `app/billing`

Интеграция с Adapty: приём вебхуков, ресинк `getProfile`, маппинг тарифа → квоты, quota-gate, grace-teardown сайтов. Adapty — источник истины по правам ([ADR-004](../../adr/ADR-004-adapty-source-of-truth.md), [ADR-009](../../adr/ADR-009-billing-idempotency-resync-grace.md)).

## Граница
- iOS ведёт покупку через Adapty SDK сам; backend только сверяет entitlements и гейтит генерацию.
- Эндпоинт вебхука авторизуется **Bearer-секретом** `ADAPTY_WEBHOOK_SECRET` (constant-time, **не HMAC**; ревизия [ADR-027](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md)). После авторизации — always-200-on-bad-input. `subscription_started`/`renewed` дополнительно начисляют токены `bonus_generations_balance` по тиру product_id (token-grant дополняет access_level-модель).
- HTTP-роутинг и подключение dependency — модуль `api`; teardown сайтов — модуль `deploy`. `billing` отдаёт `quota_gate`/`entitlements` и инициирует teardown.

## Документы
- [00-overview.md](00-overview.md) — scope, компоненты, граница S5
- [03-architecture.md](03-architecture.md) — webhook handler + маппинг событий, ресинк (beat+lazy), entitlements/quota_gate, usage_counters, grace state-machine
- [02-api-contracts.md](02-api-contracts.md) — `/billing/webhook/adapty`, `/billing/me`, quota-gate `402`

## DoD (ревизия приёма вебхука — [ADR-027](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md))
- Bearer-авторизация вебхука (`ADAPTY_WEBHOOK_SECRET`, constant-time) до парсинга тела: `401` на невалид, `500` на пустой секрет; HMAC убран.
- Always-200-on-bad-input: empty_body/invalid_json/not_an_object/missing_event_id/unknown event_type/missing_customer_user_id → `200 ignored`; `5xx` только на сбой БД.
- Дефенсивный парсинг полей; `subscription_cancelled` (status сохраняется, токены не трогаются) в нормативной таблице §2.3.
- Token-grant: `started`/`renewed` → `bonus_generations_balance += tier_tokens` (WEEKLY/YEARLY/fallback GRANT) в одной транзакции с `billing_events`+`credit_grants(created_by='adapty', idempotency_key=event_id)`; повтор event_id не начисляет повторно. Без миграции.

## DoD (Sprint 3.5)
- Идемпотентный вебхук (UNIQUE `adapty_event_id`), верификация подписи Adapty → `401` на невалид.
- Маппинг `event_type`→`subscriptions` по нормативной таблице ([03-arch §2.3](03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица)).
- `getProfile`-ресинк: периодический beat (`BILLING_RESYNC_INTERVAL_S`) + lazy на гейте; rate-limit к Adapty; fail-open на кэш.
- Сидинг `plan_quotas` (Free+Pro, Alembic data-migration).
- `entitlements` заменяют S3-заглушку free (реальный `access_level` в concurrency-cap).
- quota-gate на `POST /projects` → `402` (`reason`+`required_entitlement`); контракт на `/edits` готов (активен с S5).
- Инкремент `usage_counters` на **успешном старте генерации** (`kind='generation'`), идемпотентно по `job_id`.
- `GET /billing/me` — entitlement + остаток квоты.
- Grace-teardown: `billing.subscription_sweep` (beat) гасит сайты при `grace_until<now`; renew в grace отменяет.

## Открытые пункты
- [Q-BILLING-4](../../99-open-questions.md#q-billing-4) — open: при реальной интеграции Adapty верифицировать webhook signature-header/префикс и точную схему `getProfile` payload v2; обновить контракт-тест httpx-мока под живой sample. Не блокирует код S3.5; целевой — момент реальной интеграции / pre-prod.
- [TD-009](../../100-known-tech-debt.md#td-009) — `billing.resync` без батча/LIMIT → Sprint 6 (scale): `.limit(BATCH)` + курсор по `synced_at ASC`.
- **Связь с S4:** sandbox egress-policy не должна блокировать исходящий `getProfile` beat-воркера к Adapty — [05-security → «Граница egress-политики»](../../05-security.md#граница-egress-политики-build-sandbox-vs-application-процессы-требование-к-sprint-4), [Q-DEPLOY-1](../../99-open-questions.md#q-deploy-1).

## Changelog
- 2026-06-09: **ревизия приёма вебхука** ([ADR-027](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md)): Bearer заменяет HMAC (constant-time, `401`/`500`), always-200-on-bad-input (5xx только на сбой БД), дефенсивный парсинг, новый `subscription_cancelled`, token-grant по тиру product_id (`SUBSCRIPTION_PRODUCT_*`/`SUBSCRIPTION_TOKENS_*`/fallback `SUBSCRIPTION_TOKENS_GRANT`) с идемпотентностью по `event_id` (одна транзакция с `billing_events`+`credit_grants created_by='adapty'`, без миграции). Обновлены 02/03 модуля, 05-security, 06-testing, 07-deployment. **Код требует доработки.**
- 2026-06-02: создан bootstrap (architect).
- 2026-06-02: зафиксированы решения S3.5 — Free+Pro freemium, лимиты, grace 7д, вебхуки+ресинк, маппинг `customer_user_id=user.id` (Q-BILLING-1/2/3 resolved, [08-product-decisions.md §3.5](../../08-product-decisions.md#sprint-35--billing-adapty)).
- 2026-06-02: развёрнут **исполняемый контракт S3.5** — маппинг событий вебхука, dual-source ресинк (beat+lazy), quota-gate `reason`-коды, `usage_counters` точка инкремента, grace state-machine + sweeper, новые env-ключи, [ADR-009](../../adr/ADR-009-billing-idempotency-resync-grace.md); добавлены `subscriptions.grace_until`/`status=billing_issue` ([03-data-model](../../03-data-model.md#биллинг)).
- 2026-06-02: **Sprint 3.5 реализован** (backend+devops → reviewers → qa 417 passed / coverage 88% → reviewer `production_ready: true`). Статус модуля → «реализован». Живой E2E с реальным Adapty (боевой вебхук+подпись + `getProfile` v2) + Claude+Docker — НЕ прогонялся (проверено через httpx-мок + HMAC тест-секрет + эфемерный Postgres/Redis); открыт [Q-BILLING-4](../../99-open-questions.md#q-billing-4). Заведён [TD-009](../../100-known-tech-debt.md#td-009) (resync без батча → S6). Зафиксировано требование S4: egress-policy не блокирует `getProfile` beat-воркера ([05-security](../../05-security.md#граница-egress-политики-build-sandbox-vs-application-процессы-требование-к-sprint-4)).
