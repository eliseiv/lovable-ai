# admin — API Contracts ([ADR-021](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

Base: `https://api.domain/v1` · Auth: **`X-Admin-Key: <ADMIN_API_KEY>`** (НЕ Bearer) · Ошибки: RFC-7807 (`application/problem+json`).

> **Публичная схема (ADR-021 revision):** все эндпоинты ниже — **`include_in_schema=True`** (ВИДИМЫ в `/openapi.json` и `/docs`) под тегом **«Администрирование»**, с per-operation security **`AdminKey`** (apiKey-заголовок `X-Admin-Key`, **не** глобальный `BearerAuth`; схема объявлена в `components.securitySchemes` кастомным `app.openapi()` — [admin §4](03-architecture.md#4-публичная-openapi-adr-021-revision)). Денилист [api §B.7](../api/02-api-contracts.md#b7-чек-лист-для-reviewerqa-grep-критерии-чистоты-openapijson) применяется: docstring/`summary` — **на русском, без `Sprint`/`ADR`/`TD`/имён агентов** (`admin`/`login-as`/`X-Admin-Key` легитимны, процессные маркеры — нет). Подача наружу — [api §B.4/§B.5](../api/02-api-contracts.md#b4-группировка-по-доменам--tags-нормативный-перечень-русские-названия), [ADR-021 §C revision](../../adr/ADR-021-admin-plane-and-bonus-credits.md).

## Сводка endpoints

| Method | Path | Назначение | Auth | Success |
|---|---|---|---|---|
| POST | `/admin/login-as` | выпустить пользовательский Bearer за `user_id` (создать юзера без Apple, если нет) | `X-Admin-Key` | `200` |
| POST | `/admin/users/{user_id}/credits` | начислить/скорректировать бонус-генерации | `X-Admin-Key` | `200` |
| POST | `/admin/users/{user_id}/subscription` | выдать pro-подписку (`access_level=pro`) на срок/бессрочно ([ADR-037](../../adr/ADR-037-admin-grant-pro-subscription.md)) | `X-Admin-Key` | `200` |
| GET | `/admin/users/{user_id}` | баланс кредитов + квота юзера | `X-Admin-Key` | `200` |
| GET | `/admin/costs/daily` | дневные расходы LLM день × провайдер (расширение контракта broad-crm v1.3, [ADR-044](../../adr/ADR-044-crm-daily-costs-endpoint.md)) | `X-Admin-Key` | `200` |

## Аутентификация админ-эндпоинтов ([ADR-021 §A](../../adr/ADR-021-admin-plane-and-bonus-credits.md))
- Заголовок **`X-Admin-Key: <ADMIN_API_KEY>`**. Dependency `require_admin` сравнивает значение constant-time (`hmac.compare_digest`) с `settings.admin_api_key`.
- Невалидно/отсутствует → **`401`** RFC-7807, **без раскрытия** причины.
- **`ADMIN_API_KEY` пуст/не сконфигурирован** → `require_admin` **всегда `401`** (админ-плоскость отключена; ни один ключ не валиден). Работает одинаково в **dev И prod** — `settings.environment` не участвует.

## POST /admin/login-as
Выпуск свежего пользовательского Bearer за указанного `user_id` (dev/тест-логин без Apple Sign-In + операторская выдача токена).
- **Auth:** `X-Admin-Key`.
- **Body:**
```json
{ "user_id": "u_...?", "device_label": "string?" }
```
- **Поведение:**
  - `user_id` задан и существует → выдать токен за этого юзера.
  - `user_id` задан и не существует → создать `users` с этим `id`, `apple_sub=NULL`, `adapty_customer_user_id=users.id` (минимальный upsert, как `/auth/apple`, но без Apple-якоря — [ADR-021 §B](../../adr/ADR-021-admin-plane-and-bonus-credits.md)).
  - `user_id` опущен → сервер генерирует новый `u_...` и создаёт юзера.
  - Токен выпускается через `auth.token_service` (новая строка `api_tokens`, `device_label` по умолчанию `"admin-login"`).
- **`200`** →
```json
{ "api_key": "lv_<key_id>_<secret>",
  "token_id": "t_...",
  "user_id": "u_..." }
```
- `api_key` возвращается **один раз** (как `/auth/apple`); сервер хранит только `key_id` + argon2-хэш `secret`.
- **Ошибки:** `401` (нет/неверный `X-Admin-Key`), `422` (невалидное тело).

## POST /admin/users/{user_id}/credits
Начислить (или скорректировать) бонус-генерации юзеру **сверх** плановой месячной квоты ([ADR-021 §D](../../adr/ADR-021-admin-plane-and-bonus-credits.md), [billing §10](../billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021)).
- **Auth:** `X-Admin-Key`.
- **Headers:** `Idempotency-Key` (опц.) — дедуп начисления (UNIQUE `credit_grants(user_id, idempotency_key)`); повтор с тем же ключом → no-op, возврат текущего баланса.
- **Body:**
```json
{ "amount": 10, "reason": "string?" }
```
- **Семантика:** атомарно — insert `credit_grants` + `UPDATE users.bonus_generations_balance += amount`.
  - `amount > 0` — начисление.
  - `amount < 0` — операторская коррекция/списание. **Результирующий баланс не может стать < 0**: если `bonus_generations_balance + amount < 0` → `409` (RFC-7807, `type=.../conflict`, `detail` указывает текущий баланс), транзакция откатывается (строка `credit_grants` не пишется).
  - `amount == 0` → `422`.
- **`200`** →
```json
{ "user_id": "u_...",
  "amount_applied": 10,
  "bonus_generations_balance": 25 }
```
- **Ошибки:** `401`, `404` (нет такого `user_id`), `409` (коррекция увела бы баланс < 0), `422` (`amount==0`/невалидное тело).

## POST /admin/users/{user_id}/subscription
Выдать выбранному юзеру **pro-подписку** (`subscriptions.access_level=pro`, `status=active`) на заданный срок или бессрочно — без симуляции Adapty-вебхука ([ADR-037](../../adr/ADR-037-admin-grant-pro-subscription.md)). **Токены НЕ начисляются** (`bonus_generations_balance` не трогается) — для токенов отдельный `POST /admin/users/{user_id}/credits`.
- **Auth:** `X-Admin-Key`.
- **Body** — форма срока (поля **взаимоисключающие**, оба опциональны):
```json
{ "duration_days": 30, "expires_at": null }
```
  - `duration_days: int | null` — срок в днях от `now()` (UTC), `> 0`.
  - `expires_at: datetime | null` — явная дата окончания (ISO-8601, в будущем).
  - **Оба `null` (или тело `{}`) → бессрочно** (`subscriptions.expires_at=NULL`; не истекает ложно — гейт/sweep не консультируют `expires_at`, [ADR-037 §C](../../adr/ADR-037-admin-grant-pro-subscription.md)).
- **Семантика** ([ADR-037 §B](../../adr/ADR-037-admin-grant-pro-subscription.md), переиспользует `subscription_state.apply_admin_grant` — **не** прямой upsert): `access_level=pro`, `status=active`, `grace_until=NULL`, `will_renew=false`, `expires_at` из параметра (или `NULL`), `started_at=now()` если не задан, `synced_at=now()`, `store='admin'`, `product_id=NULL`, `raw={source:'admin_grant',...}`. Идемпотентно — одна строка `subscriptions` на `user_id` (повтор = обновление срока).
- **`200`** → `AdminUserResponse` (тот же снимок, что `GET /admin/users/{user_id}`: `access_level='pro'`, `status`, `period`, `bonus_generations_balance`, `quota{...}`).
- **Ошибки:** `401`, `404` (нет такого `user_id` — выдаём **только** существующему юзеру; в отличие от login-as, юзер не создаётся), `422` (оба поля срока заданы / `duration_days<=0` / `expires_at` не в будущем / невалидное тело).

> **Сосуществование с реальной Adapty-подпиской/ресинком** — admin-grant пишет в кэш Adapty; периодический `getProfile`-ресинк или вебхук могут перезаписать grant ([Q-ADMIN-1](../../99-open-questions.md#q-admin-1)). Предназначен для юзеров без активной реальной подписки. Срок (`expires_at`) сейчас **не энфорсится автоматически** (информативен) — снятие pro по сроку = follow-up Q-ADMIN-1.

## GET /admin/users/{user_id}
Текущий баланс кредитов + квота юзера (для операторского просмотра).
- **Auth:** `X-Admin-Key`.
- **`200`** →
```json
{ "user_id": "u_...",
  "access_level": "free",
  "status": "active",
  "period": "2026-06",
  "bonus_generations_balance": 25,
  "quota": { "monthly_generations": 3, "generations_used": 3,
             "generations_remaining": 25,
             "monthly_edits": 5, "edits_used": 1, "edits_remaining": 4,
             "max_concurrent_jobs": 1, "active_jobs": 0,
             "max_projects": 1, "projects_used": 1 } }
```
- **Источник:** те же агрегаты, что `GET /billing/me` ([billing §2](../billing/02-api-contracts.md#2-get-v1billingme)) + `users.bonus_generations_balance`, но **за указанного `user_id`** (а не за текущего Bearer). `generations_remaining = max(0, monthly_generations - generations_used) + bonus_generations_balance`. В примере: `max(0, 3-3)=0` план + `25` кредитов = `25`.
- **Ошибки:** `401`, `404` (нет такого `user_id`).

## GET /admin/costs/daily

Дневные расходы LLM инстанса с гранулярностью **день × провайдер** ([ADR-044](../../adr/ADR-044-crm-daily-costs-endpoint.md)). Реализует расширение **v1.3** контракта бэков broad-crm — путь, имена query-параметров и полей ответа заморожены на стороне CRM, менять их односторонне нельзя.

**Query:**

| Параметр | Тип | Обяз. | Семантика |
|---|---|---|---|
| `date_from` | `YYYY-MM-DD` | да | Первый день периода **включительно**, UTC |
| `date_to` | `YYYY-MM-DD` | да | Последний день периода **включительно**, UTC |
| `limit` | int `1…1000` | нет | Размер страницы, дефолт `1000` |
| `offset` | int `≥ 0` | нет | Смещение страницы, дефолт `0` |

**`200`** → `{ "total": int, "items": CrmDailyCostItem[] }`, где элемент — `{ date, provider, spend_usd, requests, tokens }`:

```json
{ "total": 3,
  "items": [
    { "date": "2026-08-10", "provider": "anthropic", "spend_usd": 0.03, "requests": 2, "tokens": 32.0 },
    { "date": "2026-08-10", "provider": "openai", "spend_usd": 0.03, "requests": 1, "tokens": 16.0 },
    { "date": "2026-08-11", "provider": "openai", "spend_usd": 0.04, "requests": 1, "tokens": 16.0 }
  ] }
```

- **Источник** — cost-ledger `llm_usage` ([03-data-model](../../03-data-model.md)): `spend_usd` = `SUM(cost_usd)`, `requests` = число записей ledger, `tokens` = `SUM(input + output + cache_read + cache_write)`.
- **`provider` выводится из `llm_usage.model`, а НЕ из `LLM_PROVIDER` инстанса** (нормативно, [ADR-044](../../adr/ADR-044-crm-daily-costs-endpoint.md)): `claude*` → `anthropic`, `gpt*` → `openai`, нераспознанная модель → её сырое имя. В ledger одного инстанса сосуществуют записи обоих провайдеров (переключение по [ADR-032](../../adr/ADR-032-llm-provider-abstraction-openai.md)), поэтому подстановка текущего провайдера переписала бы историю.
- **Сортировка** — `date ASC, provider ASC`; пара `(date, provider)` уникальна, поэтому порядок полный и `limit/offset` даёт стабильные страницы. `total` — число строк за период **до** пагинации.
- **Отсутствие строки** за `(день, провайдер)` означает «расхода не было» — нули не досыпаются.
- **Ошибки:** `400` (`date_from > date_to`; период длиннее **92** дней), `401`/`403` (админ-гейт), `422` (нераспознанная дата — стандартная валидация FastAPI).

## Конвенции ошибок (RFC-7807)
```json
{ "type": "https://api.domain/errors/unauthorized",
  "title": "Unauthorized",
  "status": 401,
  "detail": "Invalid or missing admin credentials." }
```
- Провалы `X-Admin-Key` → `401` без раскрытия причины; **отсутствие заголовка** → `403` (`require_admin`: нет заголовка → `forbidden`, неверный ключ или отключённая плоскость → `unauthorized`). Валидационные `422`/конфликтные `409` — тоже `application/problem+json` (глобальный обработчик [api §Обработчики ошибок](../api/03-architecture.md#обработчики-ошибок--rfc-7807-нормативно-все-ошибки-включая-422)).
