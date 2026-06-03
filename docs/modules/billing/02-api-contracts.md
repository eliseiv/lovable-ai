# billing — API Contracts (исполняемый контракт Sprint 3.5)

Base: `https://api.domain/v1`. Все ошибки — RFC-7807 (`application/problem+json`). Числовые значения квот в примерах **иллюстративны**; нормативный источник — `plan_quotas` ([03-data-model → plan_quotas](../../03-data-model.md#plan_quotas), [08 §3.5](../../08-product-decisions.md#sprint-35--billing-adapty)).

---

## 1. POST /v1/billing/webhook/adapty

**Server-to-server. НЕ Bearer.** Источник истины по правам ([ADR-004](../../adr/ADR-004-adapty-source-of-truth.md), [ADR-009](../../adr/ADR-009-billing-idempotency-resync-grace.md)).

### Auth
- Верификация **секрета/подписи Adapty** (header-secret или HMAC-подпись — по конфигурации Adapty webhook v2; секрет в `ADAPTY_WEBHOOK_SECRET`). Невалидно/отсутствует → `401` (без раскрытия причины). Реализация — [03-architecture §2](03-architecture.md#2-webhook-handler-post-v1billingwebhookadapty), threat-model — [05-security → Webhook forgery](../../05-security.md#threat-model-центр--build-sandbox).

### Body (схема — стабильный минимум; точный payload Adapty webhook v2 фиксируется по актуальной доке Adapty при реализации)
```json
{ "event_id": "evt_...",
  "event_type": "subscription_renewed",
  "customer_user_id": "u_...",
  "profile": { "access_level": "pro", "is_active": true },
  "subscription": { "product_id": "lovable.pro.monthly", "store": "app_store",
                    "expires_at": "2026-07-02T00:00:00Z", "will_renew": true,
                    "transaction_id": "...", "started_at": "2026-06-02T00:00:00Z" } }
```
- **Обязательные для контракта поля:** `event_id` (→ `billing_events.adapty_event_id`, UNIQUE, идемпотентность), `event_type`, `customer_user_id` (= `user.id`, [Q-BILLING-3](../../99-open-questions.md#q-billing-3)). Полный сырой payload сохраняется в `billing_events.payload` (jsonb) и `subscriptions.raw`.

### Маппинг `event_type` → `subscriptions.status`/`access_level`
Нормативная таблица событий → перехода — [03-architecture §2.3](03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица). Кратко:

| `event_type` | Эффект на `subscriptions` |
|---|---|
| `subscription_started` / `subscription_renewed` | `status=active`, `access_level` из профиля, `expires_at`/`will_renew` из payload, `grace_until=NULL` |
| `access_level_updated` (изменение уровня) | `access_level` ← новое значение; `status=active` если профиль активен |
| `subscription_expired` | `status=grace`, `grace_until = expires_at + GRACE_PERIOD_DAYS` (см. §6 grace сайтов) |
| `subscription_refunded` | `status=grace`, `grace_until = now() + GRACE_PERIOD_DAYS` |
| `billing_issue_detected` | `status=billing_issue` (на гейте трактуется как НЕ-активный, см. §4) |
| `subscription_renewed` в состоянии `grace`/`billing_issue` | `status=active`, `grace_until=NULL` (отмена pending-teardown, [03-arch §6](03-architecture.md#6-grace-период-сайтов-q-billing-1)) |

### Идемпотентность и ответы
- `event_id` уже в `billing_events` → `200` no-op (idempotent replay).
- Новый `event_id` → insert `billing_events(processed_at=NULL)` → маппинг на `user` → апдейт `subscriptions` (transactionally) → `processed_at=now()` → `200`.
- `customer_user_id` неизвестен (рассинхрон) → `billing_events(user_id=NULL, processed_at=NULL)` для последующей обработки/алерта → `200` (не теряем событие, [Q-BILLING-3](../../99-open-questions.md#q-billing-3)).
- Внутренняя ошибка обработки после валидной подписи → `5xx` (Adapty повторит доставку); строка `billing_events` остаётся `processed_at IS NULL` и будет добита ресинком/повтором.

---

## 2. GET /v1/billing/me

Auth: Bearer.
- `200` →
```json
{ "access_level": "pro",
  "status": "active",
  "period": "2026-06",
  "quota": { "monthly_generations": 100, "generations_used": 12,
             "generations_remaining": 88,
             "monthly_edits": null, "edits_used": 3, "edits_remaining": null,
             "max_concurrent_jobs": 3, "active_jobs": 0,
             "max_projects": null, "projects_used": 4 } }
```
- `status` ∈ `active` / `grace` / `billing_issue` / `expired` (см. `subscriptions.status`). `max_projects: null` = безлимит (Pro).
- **Sprint 5** ([ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)): `monthly_edits`/`edits_used`/`edits_remaining` — **отдельный лимит правок** (`plan_quotas.monthly_edits` + `edit_usage_counters` за текущий `period`). `monthly_edits: null` = безлимит (Pro) → `edits_remaining: null`; иначе `edits_remaining = max(0, monthly_edits - edits_used)`.
- **Источник:** `subscriptions` (кэш Adapty) для `access_level`/`status` + `usage_counters`/`edit_usage_counters` (текущий `period`) + `plan_quotas` (лимиты) + `COUNT` активных джоб/проектов. `generations_remaining = max(0, monthly_generations - generations_used)`.
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
