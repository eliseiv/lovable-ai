# ADR-038 — Consumable token-паки Adapty (`non_subscription_purchase`) + подписки = 0 бонус-токенов

**Статус:** Accepted · **Дата:** 2026-07-08 · **Sprint:** billing (доработка)

Уточняет/дополняет [ADR-027](ADR-027-adapty-webhook-bearer-token-grant.md) (приём Adapty webhook + token-grant по тиру): вводит **второй класс события** — разовую (consumable) покупку токен-пака — в ту же always-200/идемпотентную модель приёма. Ревизует продуктовую модель токенов подписок (§D). **Не пересматривает** Bearer-авторизацию (ADR-027 §A), always-200-on-bad-input (§B), дефенсивный парсинг (§C), идемпотентность начисления (§E), identity-контракт (§G), admin-grant ([ADR-037](ADR-037-admin-grant-pro-subscription.md)), dual-source/ресинк/grace (ADR-004/009).

## Context

Текущий billing обрабатывает **только** подписочные события Adapty (`app/billing/subscription_state.py` — `KNOWN_EVENT_TYPES` = 7 subscription_* типов; `TOKEN_GRANT_EVENT_TYPES = {started, renewed}`; `resolve_tier_tokens` маппит ровно 2 SKU подписок на токены с fallback `SUBSCRIPTION_TOKENS_GRANT`). Разовые (non-subscription / consumable) покупки **не обрабатываются** — `non_subscription_purchase` попадает в неизвестный `event_type` → `200 ignored`, токены не начисляются.

Продуктовое решение вводит **покупку токенов паками** (consumable-продукты App Store через Adapty) и меняет роль подписки:

1. **Токен-паки** (5 продуктов на старте): `100_tokens_9.99`→100, `250_tokens_19.99`→250, `500_tokens_34.99`→500, `1000_tokens_59.99`→1000, `2000_tokens_99.99`→2000 генераций в `bonus_generations_balance`. Каталог **открытый** (оператор добавляет паки в дашборде Adapty) — нужен расширяемый маппинг, а не фиксированные 2 SKU.
2. **Подписки** (`week_6.99_not_trial`, `yearly_49.99_not_trial`) дают **только** `access_level=pro` (плановая квота 100 ген/мес по `plan_quotas`) и **0 бонус-токенов** — бонус-токены за подписку не начисляются (ценность подписки = pro-доступ, а не пакет кредитов).

### Точное имя события Adapty (верифицировано)

Тип события consumable/non-subscription покупки в Adapty webhook — **`non_subscription_purchase`** (refund-вариант — `non_subscription_purchase_refunded`); идентификатор продукта в payload — **`vendor_product_id`**. Источник: официальная документация Adapty [«Webhook event types and fields»](https://adapty.io/docs/webhook-event-types-and-fields) (проверено 2026-07-08 — полный список из 18+ типов включает `non_subscription_purchase` и `non_subscription_purchase_refunded`). Имя **не выдумано** — сверено с first-party-документацией.

## Decision

### A. Новый класс события — `non_subscription_purchase` (consumable)
- В `subscription_state.KNOWN_EVENT_TYPES` добавляется `non_subscription_purchase` (константа `EVENT_NON_SUBSCRIPTION_PURCHASE`). Он **не** входит в `TOKEN_GRANT_EVENT_TYPES` (та относится к подписочному пути) — вводится отдельное множество `CONSUMABLE_EVENT_TYPES = {non_subscription_purchase}`.
- **Consumable-событие НЕ трогает `subscriptions`/`access_level`** — это разовая покупка, не подписка. В `webhook_handler.process_webhook` `event_type ∈ CONSUMABLE_EVENT_TYPES` идёт по **отдельной ветке**, минуя `apply_webhook_event` (которая создала/мутировала бы строку `subscriptions`). Единственный эффект consumable — начисление токенов по `vendor_product_id`.
- `non_subscription_purchase_refunded` **вне scope** этой итерации (в `KNOWN_EVENT_TYPES` не добавляется → `200 ignored: event_type`, no-op). Clawback потраченных consumable-кредитов рискует отрицательным балансом (пак мог быть уже израсходован) и требует отдельного продуктового решения — [Q-BILLING-6](#открытые-вопросы).

### B. Расширяемый маппинг `vendor_product_id → tokens` (env, единая строка)
- Механизм — **один env-ключ `TOKEN_PACK_PRODUCTS`** типа `str`, CSV-список пар `<vendor_product_id>:<amount>`, парсится приложением в `dict[str, int]`. Например:
  `TOKEN_PACK_PRODUCTS="100_tokens_9.99:100,250_tokens_19.99:250,500_tokens_34.99:500,1000_tokens_59.99:1000,2000_tokens_99.99:2000"`.
- **Почему единая строка, а не per-pack поля (как `SUBSCRIPTION_PRODUCT_*`/`SUBSCRIPTION_TOKENS_*`):** подписок ровно 2 фиксированных тира → пара полей на тир оправдана; каталог токен-паков **открытый** (оператор добавляет паки без правки кода). Пара полей на пак не расширяема (6-й пак = code-change). Единая строка расширяема оператором и **консистентна со стилем проекта** — flat `str`-env + ручной парсинг (прецедент `NPM_REGISTRY_ALLOWLIST: str`, CSV → список; проект не использует complex-typed JSON Settings-полей, `env_nested_delimiter="__"`).
- **Нормативные правила парсинга** (единый источник — [billing §11.3](../modules/billing/03-architecture.md#113-consumable-token-паки-non_subscription_purchase-adr-038)): разбить по `,`; каждую пару по первому `:` на `(vendor_product_id, amount)`; trim пробелов; `amount` → `int`, инвариант `amount >= 0`. Пустая строка → пустой маппинг (паки не сконфигурированы). **Fail-fast:** невалидная запись (нет `:`, нецелый/отрицательный `amount`) → ошибка загрузки конфигурации (приложение не стартует) — молчаливый drop пака означал бы «оплаченный пак не начислил токены», что хуже видимого сбоя деплоя. Оператор контролирует короткий список.
- **Env-контракт (имя символ-в-символ, тип, потребитель, x-app-env)** — [07-deployment → Consumable token-паки](../07-deployment.md#adapty-webhook--token-grant-по-тиру-adr-027--adr-038). Потребитель — `api`+`worker` (как прочие billing-ключи). product_id↔amount — внешняя зависимость дашборда Adapty (как access_level↔product и subscription-SKU).

### C. Начисление consumable-токенов — переиспользование grant-механики (без дублирования)
- Consumable-грант пишет через **тот же write-path**, что подписочный ([ADR-027 §E](ADR-027-adapty-webhook-bearer-token-grant.md), [billing §11.2](../modules/billing/03-architecture.md#112-начисление-и-идемпотентность-adr-027-e)): относительный атомарный `UPDATE users SET bonus_generations_balance += :amount` (механика admin `_apply_balance_delta`) **в ТОЙ ЖЕ транзакции**, что insert `billing_events` (UNIQUE `adapty_event_id`), плюс `credit_grants(amount, reason='adapty:non_subscription_purchase', idempotency_key=event_id, created_by='adapty')`.
- **Дедупликация write-path.** Ledger-запись выносится в общий примитив (напр. `grant_tokens(session, *, user_id, event_id, event_type, amount)`), которым пользуются обе ветки — подписочная (`amount = resolve_tier_tokens(...)`) и consumable (`amount = resolve_consumable_tokens(...)`). Существующий `grant_subscription_tokens` **не** дублируется — рефакторится на общий примитив + тонкий резолвер тира. Точная сигнатура — backend; нормативно: **одна** реализация ledger-записи на оба класса.
- **Short-circuit `amount <= 0`:** резолвер вернул `0` (или подписка с 0-токенами, §D) → грант **не** пишется (нет строки `credit_grants`, нет balance-delta); `billing_events` и (для подписок) `subscriptions` фиксируются как обычно. Устраняет мусорные нулевые ledger-строки при каждом renew подписки с нулевыми токенами.
- `resolve_consumable_tokens(vendor_product_id, settings) -> int | None`: `vendor_product_id ∈ TOKEN_PACK_PRODUCTS` → его amount; иначе → `None` (неизвестный token-product, §E). **В отличие от `resolve_tier_tokens` — БЕЗ fallback-константы:** ценность consumable = именно число токенов, угадать его нельзя; начислить произвольное число хуже, чем не начислить.

### D. Подписки → 0 бонус-токенов
- Нормативные значения токен-грантов подписок = **0**: `SUBSCRIPTION_TOKENS_WEEKLY=0`, `SUBSCRIPTION_TOKENS_YEARLY=0`, `SUBSCRIPTION_TOKENS_GRANT=0`. С учётом short-circuit (§C) `subscription_started`/`renewed` **не** начисляют бонус-токенов — только `access_level=pro`/`status` (плановая квота 100 ген/мес по `plan_quotas`, без изменений).
- Реальные product_id подписок (`week_6.99_not_trial`/`yearly_49.99_not_trial`) — **операторская env-настройка** `SUBSCRIPTION_PRODUCT_WEEKLY`/`_YEARLY` (сверка `vendor_product_id`, обязана совпадать с дашбордом Adapty), не хардкод логики. Механизм `resolve_tier_tokens` не меняется — меняются только значения (env/дефолты кода → 0).
- **Backend:** дефолты полей `subscription_tokens_weekly/yearly/grant` в `app/core/config.py` → `0`; дефолты `subscription_product_weekly/yearly` → реальные SKU (`week_6.99_not_trial`/`yearly_49.99_not_trial`). Инвариант `ge=0` сохраняется.

### E. Edge-cases и идемпотентность
- **Неизвестный `vendor_product_id` при `non_subscription_purchase`** (product_id не в `TOKEN_PACK_PRODUCTS` / отсутствует): токены **не** начисляются (не угадываем). `billing_events` записывается с `processed_at=NULL` (событие не теряется — оператор может добавить маппинг и вручную переобработать), логируется warning; alert `billing_unknown_token_product` = **Grafana-alert поверх `lovable_billing_webhook_dropped_total{reason="unknown_token_product"}`** (метрика зарегистрирована [ADR-041 §E](ADR-041-adapty-webhook-field-extraction-real-payload.md)/[observability §2.7](../modules/observability/03-architecture.md#27-billing--quota-billing) — отдельной метрики нет), ответ `200 {"status":"ignored","reason":"unknown_token_product"}`. Автопереобработки нет (getProfile-ресинк — только подписки); реобработка после правки env — ручная.
- **Неизвестный/не-маппящийся `customer_user_id`:** та же существующая ветка, что для подписок (общая, до event-type-специфичной обработки) — `billing_events(user_id=NULL, processed_at=NULL)`, `200 ignored: missing_customer_user_id`, без потери ([ADR-027 §G](ADR-027-adapty-webhook-bearer-token-grant.md)).
- **Идемпотентность — без нового механизма:** дедуп по UNIQUE `billing_events.adapty_event_id` (повтор `event_id` → `200 duplicate`, начисление не повторяется) + партиальный UNIQUE `credit_grants(user_id, idempotency_key=event_id)` как вторая страховка. Как в ADR-027 §E.
- **Без миграции и новых полей БД:** переиспользуются `users.bonus_generations_balance` + `credit_grants` (`created_by='adapty'`, `idempotency_key` уже существуют). Единственный новый env — `TOKEN_PACK_PRODUCTS`; три `SUBSCRIPTION_TOKENS_*` меняют дефолт-значение (не схему).

## Consequences

**Плюсы:** consumable-покупки монетизируются в единой always-200/идемпотентной модели приёма (ADR-027) без нового транспорта/таблиц/миграций; каталог паков расширяется оператором через один env без code-change; write-path грантов не дублируется (одна ledger-реализация на оба класса); неизвестный token-product безопасно игнорируется без произвольного начисления, событие не теряется; подписка чисто отделена от токенов (pro-доступ vs пакет кредитов).

**Минусы:** `TOKEN_PACK_PRODUCTS` — плоская строка (парсинг/валидация в коде, fail-fast на typo — риск падения старта при опечатке, mitigation: короткий контролируемый список + видимость в деплое); неизвестный token-product при забытом env → все паки `ignored` (оплата без токенов), mitigation — alert `billing_unknown_token_product`; refund consumable (`non_subscription_purchase_refunded`) не обрабатывается (Q-BILLING-6); маппинг product_id↔amount — внешняя зависимость дашборда Adapty (рассинхрон SKU → ignored + alert).

## Alternatives

- **Per-pack поля `TOKEN_PACK_<N>_PRODUCT`/`_AMOUNT` (стиль `SUBSCRIPTION_*`).** Отвергнута: не расширяема (фиксированное число паков, 6-й пак = code-change); каталог consumable — открытый, в отличие от 2 фиксированных тиров подписки.
- **JSON-объект в env → `dict[str,int]` Settings-поле (pydantic-нативный парс).** Отвергнута: проект не использует complex-typed JSON Settings-полей (стиль — flat `str` + ручной парс, `NPM_REGISTRY_ALLOWLIST`), `env_nested_delimiter="__"` усложняет complex-типы; CSV ближе к прецеденту.
- **Парсить число токенов из имени продукта (`100_tokens_9.99` → 100).** Отвергнута: жёстко связывает начисление с конвенцией именования в дашборде Adapty; опечатка/смена схемы имён → неверное начисление. Явный маппинг надёжнее.
- **Fallback-константа для неизвестного consumable (по образцу `SUBSCRIPTION_TOKENS_GRANT`).** Отвергнута: у consumable нет «безопасного» дефолта — всё значение продукта в числе токенов; произвольное начисление хуже, чем ignore+alert.
- **Обрабатывать `non_subscription_purchase_refunded` (clawback).** Отложена (Q-BILLING-6): риск отрицательного баланса на израсходованном паке, требует продуктового решения.

## Открытые вопросы

- **Q-BILLING-6** — обработка `non_subscription_purchase_refunded` (clawback consumable-токенов): списывать ли начисленные токены при возврате, как разрешать отрицательный баланс (пак израсходован). Вне scope этой итерации; не блокирует начисление паков.
- **Q-BILLING-5** (сопутствующая находка, не в scope ADR-038) — **resolved [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) (2026-07-10):** сверено с first-party доке Adapty — `subscription_cancelled` не существует, фактическое имя `subscription_renewal_cancelled`; исправлено в ADR-027 §F / `KNOWN_EVENT_TYPES` / §2.3 / data-model / tests.
