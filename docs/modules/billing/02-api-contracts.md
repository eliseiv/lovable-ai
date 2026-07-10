# billing — API Contracts (исполняемый контракт Sprint 3.5)

Base: `https://api.domain/v1`. Все ошибки — RFC-7807 (`application/problem+json`). Числовые значения квот в примерах **иллюстративны**; нормативный источник — `plan_quotas` ([03-data-model → plan_quotas](../../03-data-model.md#plan_quotas), [08 §3.5](../../08-product-decisions.md#sprint-35--billing-adapty)).

---

## 1. POST /v1/billing/webhook/adapty

**Server-to-server. Bearer-секрет вебхука ([ADR-027](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md), ревизует приёмную часть [ADR-004](../../adr/ADR-004-adapty-source-of-truth.md)/[ADR-009](../../adr/ADR-009-billing-idempotency-resync-grace.md)).** Источник истины по правам — Adapty. Это **не** пользовательский Bearer (`token_service`), а статический секрет вебхука `ADAPTY_WEBHOOK_SECRET`.

### Auth ([ADR-027 §A](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md))
- `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`, сравнение **constant-time** (`hmac.compare_digest`). HMAC-проверка подписи **убрана** с webhook-пути.
- Неверный/отсутствующий токен → `401` (без раскрытия причины).
- `ADAPTY_WEBHOOK_SECRET` пуст/не задан → `500` с понятным текстом (мисконфигурация сервера).
- **Авторизация ВСЕГДА выполняется до парсинга тела.** Реализация — [03-architecture §2](03-architecture.md#2-webhook-handler-post-v1billingwebhookadapty), threat-model — [05-security → Webhook forgery](../../05-security.md#threat-model-центр--build-sandbox).

### Body (дефенсивный парсинг — форма сверена с first-party доке Adapty, [ADR-040](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) ревизует [ADR-027 §C](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md))

> **Фактическая форма payload Adapty** (источник: <https://adapty.io/docs/webhook-event-types-and-fields>, сверка 2026-07-10): **НЕТ** верхнеуровневых `event_id`/`id`; ключ дедупа — **`profile_event_id` (UUID) ВНУТРИ `event_properties`**. Верхнеуровневые поля: `profile_id`, `customer_user_id`, `event_type`, `event_datetime`, `event_properties`, `email`, `idfv`/`idfa`/`advertising_id`, `play_store_purchase_token` и др. (полный список — [ADR-040 §Источник](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)). `event_properties` **варьируются по типу события** (`profile_event_id` не гарантирован для всех типов).

```json
{ "profile_id": "…",
  "customer_user_id": "u_...",
  "event_type": "subscription_renewed",
  "event_datetime": "2026-07-02T00:00:00Z",
  "event_properties": { "profile_event_id": "550e8400-e29b-41d4-a716-446655440000",
                        "vendor_product_id": "lovable.pro.yearly",
                        "transaction_id": "…", "original_transaction_id": "…",
                        "expires_at": "2026-07-02T00:00:00Z" } }
```
Извлечение полей (первое непустое):
- **Ключ дедупа** (→ `billing_events.adapty_event_id`, UNIQUE), резолюция по приоритету ([ADR-040 §A](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)):
  1. `event_properties.profile_event_id` (канонический UUID);
  2. `adapty-syn:{event_type}:{event_properties.transaction_id || event_properties.original_transaction_id}` (fallback, endorsed провайдером);
  3. `adapty-syn:body:{sha256(raw_body)}` (последний резерв — гарантия «никогда не дропнуть тихо»).
  Тир 2/3 (т.е. `profile_event_id` отсутствовал) → **WARN-диагностика** `reason=profile_event_id_absent` (§D). Ключ выводится **всегда** ⇒ ветки `missing_event_id` больше нет.
- `event_type` (верхний уровень) → `.lower()`
- `customer_user_id` (верхний уровень) = `customer_user_id || user_id` (**`profile.customer_user_id` удалён — объекта `profile` в payload нет, [ADR-041 §A](../../adr/ADR-041-adapty-webhook-field-extraction-real-payload.md)**; обязан = `user.id`, identity-контракт [ADR-027 §G](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md), [Q-BILLING-3](../../99-open-questions.md#q-billing-3))
- `vendor_product_id` = `event_properties.vendor_product_id || event_properties.product_id || vendor_product_id || product_id` (тир-маппинг токенов, [03-arch §11.1](03-architecture.md#111-тир-маппинг-подписок-env--токены--0-adr-038))
- **`expires_at`** (опц.) = `event_properties.subscription_expires_at || event_properties.expires_at` (**`profile.expires_at` удалён — объекта нет; `subscription_expires_at` для подписочных событий, `expires_at` для `access_level_updated`, [ADR-041 §A](../../adr/ADR-041-adapty-webhook-field-extraction-real-payload.md)**; отсутствует → preserve)
- **`access_level`** — **не в payload подписочных событий**: `subscription_started/renewed` ⇒ `pro` (однотарифная модель, [ADR-041 §B](../../adr/ADR-041-adapty-webhook-field-extraction-real-payload.md)); `access_level_updated` — по `event_properties.is_active`/`is_in_grace_period`/`is_refund`
- **`will_renew`** = `event_properties.will_renew` (для `access_level_updated`) → иначе по семантике `event_type` ([03-arch §2.3](03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица)); наличие `event_properties.cancellation_reason` ⇒ `false`
- **Инвариант «вебхук не понижает права по недостающим данным»** (preserve-on-missing): отсутствующее поле сохраняет текущее значение в `subscriptions`, не затирает `access_level→free`/`expires_at→NULL`/`will_renew→false` ([ADR-041 §C](../../adr/ADR-041-adapty-webhook-field-extraction-real-payload.md)).

Полный сырой payload сохраняется в `billing_events.payload` (jsonb) и `subscriptions.raw`. Упорядочивание событий — по `billing_events.received_at` (**наше** время приёма), не по `event_datetime` (рекомендация провайдера, [ADR-040 §A](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)).

### Маппинг `event_type` → `subscriptions.status`/`access_level`
Нормативная таблица событий → перехода — [03-architecture §2.3](03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица). Кратко:

| `event_type` | Эффект на `subscriptions` |
|---|---|
| `subscription_started` / `subscription_renewed` | `status=active`, `access_level=pro` (константа платного тира, [ADR-041 §B](../../adr/ADR-041-adapty-webhook-field-extraction-real-payload.md)), `expires_at`=`event_properties.subscription_expires_at`, `will_renew=true`, `grace_until=NULL` **+ token-grant по тиру** (=0, [03-arch §11.1](03-architecture.md#111-тир-маппинг-подписок-env--токены--0-adr-038)) |
| `access_level_updated` | по `event_properties.is_active`/`is_in_grace_period`/`is_refund`: `is_active=true`→`access_level=pro`,`status=active`(`grace` если `is_in_grace_period`); `is_refund=true`→`grace`; `is_active=false` (не refund) → `status` **не форсируется** (teardown по `subscription_expired`→grace→sweep, не обход grace); **`access_level` не понижается** (preserve, [ADR-041 §B/§C](../../adr/ADR-041-adapty-webhook-field-extraction-real-payload.md)) |
| `subscription_renewal_cancelled` ([ADR-027 §F](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md), имя исправлено [ADR-040 §C](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)) | подписка не продлится (`will_renew=false`), доступ по grace-семантике; **токены не трогаем** |
| `subscription_expired` | `status=grace`, `grace_until = expires_at + GRACE_PERIOD_DAYS` (см. §6 grace сайтов); без начисления |
| `subscription_refunded` | `status=grace`, `grace_until = now() + GRACE_PERIOD_DAYS` |
| `billing_issue_detected` | `status=billing_issue` (на гейте трактуется как НЕ-активный, см. §4) |
| `subscription_renewed` в состоянии `grace`/`billing_issue` | `status=active`, `grace_until=NULL` (отмена pending-teardown, [03-arch §6](03-architecture.md#6-grace-период-сайтов-q-billing-1)) |
| `non_subscription_purchase` ([ADR-038](../../adr/ADR-038-adapty-consumable-token-packs.md)) | **разовая покупка токен-пака — НЕ подписка:** `subscriptions`/`access_level` не трогаются; начисление токенов по `vendor_product_id` (`TOKEN_PACK_PRODUCTS`, [03-arch §11.3](03-architecture.md#113-consumable-token-паки-non_subscription_purchase-adr-038)); неизвестный product_id → `ignored: unknown_token_product` |

### Коды ответов и always-200-on-bad-input ([ADR-027 §B](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md))
**После успешной Bearer-авторизации НИКОГДА не возвращаем `5xx` на кривой ввод** (иначе Adapty ретраит бесконечно). `5xx` — только при реальном внутреннем сбое (БД).

| Условие | Код | Тело |
|---|---|---|
| нет/неверный Bearer | `401` | без раскрытия причины |
| `ADAPTY_WEBHOOK_SECRET` пуст/не задан | `500` | понятный текст мисконфигурации |
| пустое тело | `200` | `{"status":"ignored","reason":"empty_body"}` |
| не-JSON | `200` | `{"status":"ignored","reason":"invalid_json"}` |
| JSON не объект | `200` | `{"status":"ignored","reason":"not_an_object"}` |
| осознанно игнорируемый известный `event_type` ([ADR-040 §C.3](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md): `trial_*`/`subscription_paused`/`subscription_deferred`/`subscription_renewal_reactivated`/`entered_grace_period`/`non_subscription_purchase_refunded`) | `200` | `{"status":"ignored","event_type":"<type>"}` + INFO `reason=unhandled_known_event`; `billing_events processed_at=NULL` |
| неизвестный `event_type` (вне 18 фактических типов Adapty) | `200` | `{"status":"ignored","event_type":"<type>"}` + WARN `reason=unknown_event` |
| нет `customer_user_id` / юзер не найден (рассинхрон identity) | `200` | `{"status":"ignored","reason":"missing_customer_user_id"}` (+ событие в ledger `user_id=NULL` для ресинка) |
| `non_subscription_purchase` с неизвестным `vendor_product_id` ([ADR-038](../../adr/ADR-038-adapty-consumable-token-packs.md)) | `200` | `{"status":"ignored","reason":"unknown_token_product"}` (+ событие в ledger `processed_at=NULL`, alert; токены не начисляются) |
| валидное событие применено | `200` | `{"status":"applied",...}` |
| повтор ключа дедупа `profile_event_id`/synthetic (idempotent replay) | `200` | `{"status":"duplicate"}` |
| реальный внутренний сбой (БД) | `5xx` | Adapty повторит; строка `billing_events` остаётся `processed_at IS NULL`, добивается ресинком |

Response-схема: `{ "status": "applied"|"ignored"|"duplicate", "reason"?: string, "event_type"?: string }`.

### Идемпотентность и применение
- Ключ дедупа (`profile_event_id`/synthetic, [ADR-040 §A](../../adr/ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)) уже в `billing_events.adapty_event_id` → `200 duplicate` (idempotent replay) — начисление токенов **не** повторяется.
- Новый ключ дедупа → insert `billing_events(processed_at=NULL)` → маппинг на `user` → апдейт `subscriptions` + (для started/renewed) token-grant — **в одной транзакции** → `processed_at=now()` → `200 applied`.
- `customer_user_id` неизвестен/не маппится (рассинхрон identity) → `billing_events(user_id=NULL, processed_at=NULL)` для последующей обработки/алерта → `200 ignored` (`missing_customer_user_id`); не теряем событие ([Q-BILLING-3](../../99-open-questions.md#q-billing-3), [ADR-027 §G](../../adr/ADR-027-adapty-webhook-bearer-token-grant.md)).

---

## 2. GET /v1/billing/me

Auth: Bearer.
- `200` →
```json
{ "access_level": "pro",
  "status": "active",
  "period": "2026-06",
  "quota": { "monthly_generations": 100, "generations_used": 12,
             "bonus_generations_remaining": 10,
             "generations_remaining": 98,
             "monthly_edits": null, "edits_used": 3, "edits_remaining": null,
             "max_concurrent_jobs": 3, "active_jobs": 0,
             "max_projects": null, "projects_used": 4 } }
```
- `status` ∈ `active` / `grace` / `billing_issue` / `expired` (см. `subscriptions.status`). `max_projects: null` = безлимит (Pro).
- **Бонус-генерации ([ADR-021](../../adr/ADR-021-admin-plane-and-bonus-credits.md)):** `bonus_generations_remaining` = `users.bonus_generations_balance` (накопительный баланс кредитов, начисляемых админом сверх плановой квоты; **не** обнуляется помесячно). `generations_remaining = max(0, monthly_generations - generations_used) + bonus_generations_remaining` — суммарно доступные генерации (плановый остаток + кредиты). Списание на старте генерации тратит плановую квоту первой, затем кредиты ([03-architecture §10](03-architecture.md#10-бонус-генерации-кредиты-adr-021)). В примере: `max(0, 100-12)=88` план + `10` кредитов = `98`.
- **Sprint 5** ([ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)): `monthly_edits`/`edits_used`/`edits_remaining` — **отдельный лимит правок** (`plan_quotas.monthly_edits` + `edit_usage_counters` за текущий `period`). `monthly_edits: null` = безлимит (Pro) → `edits_remaining: null`; иначе `edits_remaining = max(0, monthly_edits - edits_used)`.
- **Источник:** `subscriptions` (кэш Adapty) для `access_level`/`status` + `usage_counters`/`edit_usage_counters` (текущий `period`) + `plan_quotas` (лимиты) + `users.bonus_generations_balance` (кредиты, [ADR-021](../../adr/ADR-021-admin-plane-and-bonus-credits.md)) + `COUNT` активных джоб/проектов. `generations_remaining = max(0, monthly_generations - generations_used) + bonus_generations_remaining`.
- Нет подписки/нет строки `subscriptions` → дефолт `access_level: "free"`, `status: "active"`, квота free-тарифа из `plan_quotas`.
- **Lazy-ресинк:** если `subscriptions.synced_at` старше TTL (`BILLING_RESYNC_INTERVAL_S`) — best-effort `getProfile` перед ответом (не блокирует при недоступности Adapty: отдаём кэш). [03-arch §3](03-architecture.md#3-ресинк-getprofile).

> Значения квот в примере иллюстративны; endpoint отдаёт фактические из `plan_quotas`. Single normative source чисел — `plan_quotas`/§3.5; здесь второго источника чисел не заводим.

---

## 3. Quota-gate на POST /v1/projects и POST /v1/projects/{pid}/edits

Не отдельный endpoint — **FastAPI-dependency** (`app/billing/quota_gate`), подключаемая модулем `api` к роутам. Контракт энфорса — [03-architecture §4](03-architecture.md#4-entitlements--quota-gate).

- **В S3.5 реально активна на `POST /v1/projects`.** На `POST /v1/projects/{pid}/edits` — тот же контракт (параметризованный `kind=edit`), активируется в **Sprint 5** ([03-arch §7](03-architecture.md#7-граница-s5-edits), [ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)).
- Проверки (любое нарушение → `402`):
  1. `access_level` активен (`status ∈ {active, grace}`; `billing_issue`/`expired` → `402`).
  2. `max_projects` не превышен (только `POST /projects`; `NULL`=безлимит).
  3. `max_concurrent_jobs` не превышен (`active_jobs(user)` — см. [auth §6](../auth/03-architecture.md), теперь по реальному `access_level`).
  4. **Бизнес-квота по `kind`:** `POST /projects` (`kind=generation`) → `generations_used < monthly_generations`; `POST /edits` (`kind=edit`, S5) → `edits_used < monthly_edits` (отдельный счётчик `edit_usage_counters`, [ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)). Rollback квотой не гейтится.

### Ответ при нарушении — `402 Payment Required` (RFC-7807)
```json
{ "type": "https://api.domain/errors/payment-required",
  "title": "Payment Required",
  "status": 402,
  "detail": "Monthly generation quota exhausted (3/3 used on free plan).",
  "required_entitlement": "pro",
  "reason": "quota_exhausted" }
```
- `reason` ∈ `no_entitlement` (нет активной подписки) / `quota_exhausted` (генерации) / `edit_quota_exhausted` (правки, S5 — [ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)) / `project_limit` (`max_projects`) / `concurrency_limit` (`max_concurrent_jobs`).
- `required_entitlement` — минимальный access_level, снимающий ограничение (обычно `pro`). iOS по этому коду показывает Adapty-пейвол.

> `concurrency_limit` исторически в S3 отдавался как `429`/`402` из `auth` ([auth §6](../auth/03-architecture.md)). В S3.5 канонизируется как `402` с `reason=concurrency_limit` (единый payment-gate), `429` остаётся за rate-limit (60/min). См. [03-arch §4](03-architecture.md#4-entitlements--quota-gate).

---

## 4. Прямой StoreKit-путь ([ADR-039](../../adr/ADR-039-direct-storekit-jws-purchase-path.md))

Клиентские эндпоинты **прямого** канала покупок параллельно Adapty-вебхуку. Оба — **пользовательский Bearer** (`token_service`, как весь клиентский API); тело — подписанная StoreKit 2 JWS-транзакция. Верификация — собственный верификатор `app/billing/storekit.py` ([03-arch §13.1](03-architecture.md#131-jws-верификатор-appbillingstorekitpy)). **Начисление — на аутентифицированного `user_id` (Bearer), НЕ на account из payload.**

### Общее

- **Auth:** `Authorization: Bearer <api-key>`; нет/невалидный → `401` (RFC-7807).
- **Body:** `{ "jws": "<signed StoreKit 2 transaction>" }`.
- **Верификация (fail-closed):** x5c cert-chain → доверенный Apple root (`APPSTORE_ROOT_CERT_DIR`) → ES256-подпись leaf → payload (`bundleId==APPSTORE_BUNDLE_ID` если задан; `environment`; `revocationDate`). Любой отказ / **roots не сконфигурированы** → **`422`** (RFC-7807, `type=…/errors/invalid-storekit-transaction`, крипто-детали не раскрываются).
- **Идемпотентность — глобальная** по `store_transactions.transaction_id` (PK): повтор той же транзакции (любым `user_id`) → `200 {"status":"duplicate"}`, без повторного начисления.
- **`5xx`** — только реальный сбой БД.
- Response-схема бизнес-исходов: `{ "status": "applied"|"duplicate"|"ignored", "reason"?: string, ... }`.

### 4.1 POST /v1/tokens/purchase

Consumable токен-пак → начисление токенов в `bonus_generations_balance`. Маппинг `product_id → tokens` — **тот же** `TOKEN_PACK_PRODUCTS`/`resolve_consumable_tokens`, что consumable-путь Adapty ([03-arch §11.3](03-architecture.md#113-consumable-token-паки-non_subscription_purchase-adr-038)); отдельного env нет.

| Условие | Код | Тело |
|---|---|---|
| нет/невалидный Bearer | `401` | RFC-7807 |
| невалидный JWS / roots не сконфигурированы (fail-closed) | `422` | RFC-7807 (`invalid-storekit-transaction`) |
| `product_id ∉ TOKEN_PACK_PRODUCTS` | `200` | `{"status":"ignored","reason":"unknown_token_product"}` |
| транзакция `revoked` | `200` | `{"status":"ignored","reason":"revoked"}` |
| повтор `transaction_id` | `200` | `{"status":"duplicate"}` |
| применено | `200` | `{"status":"applied","tokens_granted":<int>}` |
| сбой БД | `5xx` | клиент повторит |

Начисление: insert `store_transactions(kind='tokens_purchase')` + `grant_tokens(created_by='storekit', reason='storekit:tokens_purchase', idempotency_key='storekit:'+transaction_id)` — одна транзакция ([03-arch §13.2](03-architecture.md#132-начисление-и-идемпотентность)).

### 4.2 POST /v1/subscription/sync

Подписка → `access_level=pro`/`status=active`/`expires_at` из транзакции (helper `apply_storekit_subscription`, [03-arch §13.2](03-architecture.md#132-начисление-и-идемпотентность)). Токены **не** начисляет. Natural-idempotent (state-set); renewal = новая `transaction_id`.

| Условие | Код | Тело |
|---|---|---|
| нет/невалидный Bearer | `401` | RFC-7807 |
| невалидный JWS / roots не сконфигурированы | `422` | RFC-7807 (`invalid-storekit-transaction`) |
| транзакция `revoked` | `200` | `{"status":"ignored","reason":"revoked"}` |
| `expires_at` в прошлом | `200` | `{"status":"ignored","reason":"expired"}` |
| повтор `transaction_id` | `200` | `{"status":"duplicate"}` |
| применено | `200` | `{"status":"applied","access_level":"pro","expires_at":<iso\|null>}` |
| сбой БД | `5xx` | клиент повторит |

> **Публичная OpenAPI:** оба эндпоинта — клиентские (тег «Биллинг»), русскоязычные `summary`/`description` без внутренних маркеров (`Sprint`/`ADR`/`TD`/имена агентов), denylist B.7 ([api §Публичная API-документация](../api/02-api-contracts.md#публичная-api-документация-swaggeropenapi--нормативный-стандарт)). Верификатор/`store_transactions`/имена env в публичной схеме не фигурируют.

> **Сосуществование с Adapty** (без двойного начисления), per-instance безопасность (Xcode-сертификат не на prod), fail-closed — [03-arch §13.4](03-architecture.md#134-сосуществование-с-adapty--без-двойного-начисления) / [05-security → StoreKit](../../05-security.md#прямой-storekit-jws-путь-adr-039) / [07-deployment → StoreKit](../../07-deployment.md#прямой-storekit-путь-adr-039).
