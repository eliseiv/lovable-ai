# ADR-027 — Ревизия приёма Adapty webhook: Bearer вместо HMAC + token-grant по тиру + always-200-on-bad-input

**Статус:** Accepted · **Дата:** 2026-06-09 · **Sprint:** 3.5 (ревизия)

Уточняет/ревизует приёмную часть [ADR-004](ADR-004-adapty-source-of-truth.md) и [ADR-009](ADR-009-billing-idempotency-resync-grace.md) (модель приёма вебхука `POST /v1/billing/webhook/adapty`) и **интегрирует** бонус-кредитную модель [ADR-021](ADR-021-admin-plane-and-bonus-credits.md) в поток подписок. **Не пересматривает** dual-source/ресинк/grace-teardown (ADR-009 §B/§C остаются в силе) и не меняет admin-плоскость начисления (ADR-021).

## Context

Существующий приёмник вебхука (ADR-009 §A) верифицировал **HMAC-подпись/секрет Adapty**, отвечал `5xx` при внутренней ошибке после валидной подписи, и **не** начислял генерации (токены `bonus_generations_balance` начислял только админ, ADR-021). По эксплуатации выявлены три проблемы:

1. **HMAC-приём хрупок к конфигурации webhook v2 Adapty** (формат подписи варьируется); оператор требует простую, контролируемую им авторизацию.
2. **`5xx` на кривой ввод → бесконечные ретраи Adapty.** Adapty ретраит доставку при не-2xx; любой парс-сбой/неизвестный event_type/кривой payload, отдающий `5xx`, провоцирует штормовой ретрай. Контракт оператора: **5xx только при реальном внутреннем сбое (БД)**, любой невалидный вход после авторизации → `200 ignored`.
3. **Подписка не даёт генерации.** Продуктово оплата недельной/годовой подписки должна **дополнительно** (сверх access_level/quota-gate) начислять пакет генераций в `bonus_generations_balance` по тиру product_id.

## Decision

### A. Bearer заменяет HMAC на webhook-пути
- Авторизация — `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`, сравнение **constant-time** (`hmac.compare_digest`). HMAC-проверка подписи с webhook-пути **убирается**.
- Неверный/отсутствующий токен → `401` без раскрытия причины.
- `ADAPTY_WEBHOOK_SECRET` пуст/не задан → `500` с понятным текстом (мисконфигурация сервера, а не клиента; **до** парсинга тела).
- **Авторизация ВСЕГДА выполняется до парсинга тела.** Назначение Adapty Bearer-токена webhook v2 на стороне Adapty — внешняя конфигурация (как product IDs).

### B. Always-200-on-bad-input (после успешной авторизации)
**Никогда `5xx` на кривой ввод.** После прохождения Bearer-авторизации любой невалидный payload → `200` с телом `{"status":"ignored","reason":...}` (или `{"status":"ignored","event_type":...}`):

| Условие | Ответ |
|---|---|
| пустое тело | `200 {"status":"ignored","reason":"empty_body"}` |
| не-JSON | `200 {"status":"ignored","reason":"invalid_json"}` |
| JSON не объект | `200 {"status":"ignored","reason":"not_an_object"}` |
| нет `event_id` (после дефенсив-извлечения) | `200 {"status":"ignored","reason":"missing_event_id"}` |
| неизвестный `event_type` | `200 {"status":"ignored","event_type":"<type>"}` |
| нет `customer_user_id` (после дефенсив-извлечения) | `200 {"status":"ignored","reason":"missing_customer_user_id"}` |
| `customer_user_id` не маппится на `user` (рассинхрон identity) | `200 {"status":"ignored","reason":"missing_customer_user_id"}` + событие в ledger (`user_id=NULL`) для ресинка |
| валидное событие применено | `200 {"status":"applied",...}` |
| повтор `event_id` | `200 {"status":"duplicate"}` |

`5xx` — **только** при реальном внутреннем сбое (например, недоступность БД при коммите транзакции).

### C. Дефенсивный парсинг (поля разбросаны по версиям SDK)
- `event_id` = `event_id || id`
- `event_type` → `.lower()`
- `customer_user_id` = `customer_user_id || profile.customer_user_id || user_id`
- `vendor_product_id` = `event_properties.vendor_product_id || event_properties.product_id || vendor_product_id || product_id`
- `expires_at` (опц.) = `event_properties.expires_at || profile.expires_at`

### D. Token-grant ДОПОЛНЯЕТ (не заменяет) access_level-модель
- `subscription_started` / `subscription_renewed` → как раньше ставят `status=active`, `access_level` из профиля, `expires_at`/`will_renew` (ADR-009 §2.3 — существующий quota-gate сохраняется) **И** дополнительно начисляют генерации `bonus_generations_balance += tier_tokens` по тиру `vendor_product_id`.
- Тир-маппинг (env, §07-deployment): `SUBSCRIPTION_PRODUCT_WEEKLY` → `SUBSCRIPTION_TOKENS_WEEKLY`; `SUBSCRIPTION_PRODUCT_YEARLY` → `SUBSCRIPTION_TOKENS_YEARLY`; неизвестный product_id → fallback `SUBSCRIPTION_TOKENS_GRANT`.
- Существующая `access_level`/`plan_quotas`-модель остаётся неизменной. Кредиты — **сверх** плановой квоты (ADR-021 §модель), плановая квота тратится первой.

### E. Идемпотентность начисления (одна транзакция с ledger)
- Начисление — относительный атомарный `UPDATE users SET bonus_generations_balance = bonus_generations_balance + :tier_tokens` (та же механика, что admin `_apply_balance_delta`), **в ТОЙ ЖЕ транзакции**, что insert `billing_events` (UNIQUE `adapty_event_id`), плюс запись `credit_grants(amount=tier_tokens, created_by='adapty', idempotency_key=event_id)`.
- Повтор `event_id` отбивается UNIQUE `billing_events.adapty_event_id` (ADR-009 §A) → начисление **не** повторяется → `200 duplicate`. Дополнительный партиальный UNIQUE `credit_grants(user_id, idempotency_key)` — вторая страховка от двойного начисления.
- **Миграция не требуется:** `credit_grants.created_by` (text NOT NULL default 'admin') и `credit_grants.idempotency_key` (партиальный UNIQUE) уже существуют ([03-data-model → credit_grants](../03-data-model.md#credit_grants-бонус-генерации-adr-021), миграция `20260604_0001`); `created_by='adapty'` — новое значение существующей колонки, не новая схема.

### F. Новый event_type `subscription_cancelled`
- Семантика: подписка не продлится (`will_renew=false`), доступ — по существующей grace-семантике (как `subscription_expired`-ветка по модели access_level/status), **токены не трогаем**. `subscription_expired` — без начисления, существующая логика.
- Нормативная таблица event_type→status ([modules/billing/03-architecture.md §2.3](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица)) дополняется строкой `subscription_cancelled`.

### G. Identity-контракт
Adapty `customer_user_id` обязан **=** наш `user_id` (тот, что выдаёт register/login/Apple-вход). iOS вызывает `Adapty.identify(<этот id>)`. Несовпадение → вебхук не находит юзера → `200 ignored` (`missing_customer_user_id`), событие в ledger (`user_id=NULL`) для последующего ресинка/алерта, **НЕ** `5xx`. Это уточнение [Q-BILLING-3](../99-open-questions.md#q-billing-3) (identity-маппинг) под новую модель приёма.

## Consequences

**Плюсы:** простая контролируемая оператором авторизация (Bearer вместо вариативного HMAC); устранён шторм ретраев Adapty (5xx только на реальный сбой БД); подписка даёт пакет генераций без ручного админ-начисления; начисление идемпотентно (UNIQUE event_id, одна транзакция с ledger); миграция не нужна.

**Минусы:** Bearer-секрет webhook должен совпадать с настроенным в дашборде Adapty (внешняя конфигурация — риск рассинхрона секрета, mitigation — `401` диагностируется по логам); тир-маппинг по product_id требует поддержки списка SKU в env при добавлении новых тиров (fallback `SUBSCRIPTION_TOKENS_GRANT` страхует неизвестный SKU — начисление не теряется); always-200 скрывает невалидный ввод от Adapty-ретраев → диагностика только по нашим логам/`billing_events`.

## Alternatives

- **Сохранить HMAC-подпись.** Отвергнута: вариативность формата webhook v2 Adapty, оператор требует простую авторизацию своим секретом.
- **5xx на невалидный ввод (как было).** Отвергнута: провоцирует бесконечные ретраи Adapty; контракт оператора — 5xx только на реальный сбой БД.
- **Token-grant заменяет access_level-модель (чистая кредитная биллинг-модель).** Отвергнута: ломает существующий quota-gate/`plan_quotas`; решение — кредиты **дополняют** access_level, не заменяют.
- **Начисление отдельной транзакцией после ledger.** Отвергнута: окно двойного/потерянного начисления при сбое между транзакциями; атомарность ledger+grant в одной транзакции проще и надёжнее.
- **Новая таблица для adapty-начислений.** Отвергнута: `credit_grants` уже имеет `created_by`+`idempotency_key` — реюз без миграции.
