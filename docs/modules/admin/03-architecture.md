# admin — Architecture ([ADR-021](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

Операторская плоскость: один секрет `ADMIN_API_KEY` (не RBAC), login-as, бонус-кредиты. Работает в dev и prod; безопасность — секрет, **не** среда.

## Слои (`app/admin`)
- **dependency `require_admin`** — аутентификация админ-эндпоинтов по `X-Admin-Key`.
- **login-as** — выпуск пользовательского Bearer за `user_id` (через `auth.token_service`), upsert юзера без `apple_sub`.
- **credits** — начисление/коррекция бонус-генераций (ledger `credit_grants` + `users.bonus_generations_balance`); чтение баланса+квоты.

## 1. `require_admin` ([ADR-021 §A](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

```mermaid
flowchart TB
    A[запрос с X-Admin-Key] --> B{ADMIN_API_KEY сконфигурирован непустой?}
    B -->|нет| E[401 RFC-7807 — плоскость отключена]
    B -->|да| C{hmac.compare_digest header == ADMIN_API_KEY}
    C -->|fail / нет заголовка| E
    C -->|ok| D[пропуск к админ-эндпоинту]
```

- Заголовок — **`X-Admin-Key`** (отдельный от `Authorization` — не конфликтует с Bearer-парсингом `current_user`).
- Сравнение — **constant-time** `hmac.compare_digest(provided, settings.admin_api_key.get_secret_value())` (stdlib `hmac`, без новой зависимости).
- **Пустой `ADMIN_API_KEY`** (`None`/`""`) → всегда `401` (один код-путь; `compare_digest` против пустого никогда не проходит). Совместно с `include_in_schema=False` (см. §4) — эндпоинты скрыты и недоступны.
- Провал → `401` RFC-7807 без раскрытия причины (как auth-провалы `current_user`).
- **Среда не гейтит** — `settings.environment` в `require_admin` не используется (dev И prod).

## 2. Login-as ([ADR-021 §B](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

`POST /v1/admin/login-as` (`require_admin`):
1. Резолв юзера по `body.user_id`:
   - найден → берём его;
   - не найден (или `user_id` опущен) → создаём `users` (`id` = переданный или сгенерированный `u_...`, **`apple_sub=NULL`**, `adapty_customer_user_id=users.id`, `status='active'`, `bonus_generations_balance=0`). Минимальный upsert — зеркалит создание в `/auth/apple`, но без Apple-якоря.
2. `auth.token_service`: генерация `key_id`+`secret`, insert `api_tokens` (`key_hash=argon2id(secret)`, `device_label` = `body.device_label` или `"admin-login"`).
3. Ответ `{ api_key: "lv_<key_id>_<secret>", token_id, user_id }` — ключ **один раз** (как `/auth/apple`).

> **`apple_sub=NULL` для admin-created юзеров** — расширение инварианта S1 (раньше NULL только для seed-юзера). UNIQUE по `apple_sub` сохраняется (NULL не нарушает UNIQUE в Postgres). [03-data-model → users](../../03-data-model.md#users).

## 3. Бонус-генерации (кредиты, [ADR-021 §D](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

Модель и семантика списания — единый нормативный источник [billing §10](../billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021); data-model — [credit_grants](../../03-data-model.md#credit_grants-бонус-генерации-adr-021) + `users.bonus_generations_balance`. Здесь — админ-точки записи:

- **`POST /v1/admin/users/{user_id}/credits`** `{ amount, reason? }`:
  - `404` если `user_id` нет.
  - **Идемпотентность:** `Idempotency-Key` → если строка `credit_grants(user_id, idempotency_key)` уже есть, no-op, вернуть текущий баланс.
  - Атомарно в одной транзакции: `INSERT credit_grants(amount, reason, idempotency_key, created_by='admin')` + `UPDATE users SET bonus_generations_balance = bonus_generations_balance + :amount`.
  - **Инвариант `>= 0`:** при `amount < 0` и `balance + amount < 0` → `409`, rollback (строка не пишется). `amount == 0` → `422`.
- **`GET /v1/admin/users/{user_id}`** — те же агрегаты, что `GET /billing/me` ([billing §2](../billing/02-api-contracts.md#2-get-v1billingme)) + `bonus_generations_balance`, но за указанного `user_id`. `404` если нет.

> Списание кредитов (на старте генерации) — **не** здесь: атомарный декремент `users.bonus_generations_balance` на квота-гейте/usage ([billing §10.3](../billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021)), без строки `credit_grants`. `credit_grants` — только входящие начисления/коррекции.

## 4. Публичная OpenAPI ([ADR-021 §C](../../adr/ADR-021-admin-plane-and-bonus-credits.md))
- Все `/v1/admin/*` — **`include_in_schema=False`** (как `/metrics`/`/healthz`, [api §B.5](../api/02-api-contracts.md#b5-скрытие-служебных--internal-эндпоинтов-из-публичной-схемы)). В `/openapi.json` их нет → grep-чек-лист [api §B.7](../api/02-api-contracts.md#b7-чек-лист-для-reviewerqa-grep-критерии-чистоты-openapijson) не затрагивается.
- Внутренние docstring/`summary` — русский, без `Sprint`/`ADR`/`TD` (страховка на случай отзыва скрытия).

## Конвенции
- `ADMIN_API_KEY` в логах **никогда** не печатается (как Bearer-секрет). В Sentry — scrubbing (добавить в denylist, [05-security → Observability](../../05-security.md#observability-как-security-сигнал)).
- Префиксный opaque ID кредит-гранта: `cg_`.
