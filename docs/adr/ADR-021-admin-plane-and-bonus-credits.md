# ADR-021 — Админ-плоскость (ADMIN_API_KEY) + бонус-генерации (кредиты)

- **Статус:** Accepted (с ревизией §C — см. «Revision» ниже)
- **Дата:** 2026-06-04
- **Связанные:** [ADR-007](ADR-007-sign-in-with-apple.md) (Sign in with Apple), [ADR-008](ADR-008-indexed-api-key-lookup.md) (формат токена `lv_<key_id>_<secret>`), [ADR-009](ADR-009-billing-idempotency-resync-grace.md) (квота-гейт/idempotency), [ADR-014](ADR-014-edit-limit-revision-rollback.md) (отдельные счётчики).

> **Revision (ADR-021 revision, 2026-06-17) — §C пересмотрен: админ-плоскость ВИДИМА в публичной OpenAPI.** Первоначальное решение §C (`include_in_schema=False`, админ скрыт) **отозвано**. Действующее состояние (отражено в коде `app/api/routers/admin.py`, закреплено контракт-тестом `tests/contract/test_admin_openapi_visible.py`): все `/v1/admin/*` **видимы** в публичной схеме под тегом **«Администрирование»** (`include_in_schema=True`); per-operation security **`AdminKey`** (apiKey-заголовок `X-Admin-Key`, объявлена в `components.securitySchemes`) навешивается кастомным `app.openapi()` в `main.py` (вместо глобального `BearerAuth`). Защита `require_admin`/`X-Admin-Key` (§A) **не меняется** — она действует при любой видимости в схеме. Денилист утечки внутренних маркеров ([api §B.7](../modules/api/02-api-contracts.md#b7-чек-лист-для-reviewerqa-grep-критерии-чистоты-openapijson)) по-прежнему обязателен: `admin`/`login-as`/`X-Admin-Key` — легитимная публичная админ-поверхность, но `Sprint`/`ADR-`/`TD-`/имена агентов в схеме запрещены. Нормативная подача — [api §B.4/§B.5](../modules/api/02-api-contracts.md#b4-группировка-по-доменам--tags-нормативный-перечень-русские-названия). Ниже §C приведён к этому состоянию; зачёркнутый текст сохранён как история решения.

## Context

Появилась потребность в **операторской админ-плоскости** поверх существующего user-facing API:

1. **Аутентификация админ-эндпоинтов** отдельным от пользовательского Bearer механизмом (оператор сервиса, не iOS-клиент). Существующий auth — только Sign in with Apple (`POST /v1/auth/apple`) + legacy seed-ключ; ролей/`is_admin` в модели `users` нет, вводить RBAC-роли ради одного оператора — оверинжиниринг.
2. **Login по `user_id`** — выпуск пользовательского Bearer-токена за указанного юзера без Apple Sign-In: dev/тест-логин (нет Apple-флоу вне iOS) + операторская выдача токена.
3. **Бонус-генерации (кредиты)** — начисляемый админом сверх плановой месячной квоты «запас» генераций, который **не обнуляется помесячно** (в отличие от `usage_counters`), с аудитом и идемпотентностью начисления.

Ограничения: решение должно работать в **обеих средах** (dev и prod) — безопасность через секрет, **не** через env-гейтинг (`settings.environment` в auth не используется и не вводится в гейт). Публичная Swagger-схема — справочник для iOS-разработчика (B.2 denylist, B.5 hide internal); админ-плоскость наружу не предназначена.

## Decision

### A. Аутентификация админ-эндпоинтов — `ADMIN_API_KEY` + dependency `require_admin`

- Новый env-секрет **`ADMIN_API_KEY`** (`SecretStr|None`, потребитель — `api`). Единственный секрет админ-плоскости; ролей в БД не вводим.
- FastAPI-dependency **`require_admin`** (по аналогии с `current_user`/webhook-верификацией):
  - канонический заголовок — **`X-Admin-Key: <ADMIN_API_KEY>`** (отдельный заголовок, **не** `Authorization` — не конфликтует с Bearer-парсингом `current_user` и не смешивает две схемы аутентификации);
  - сравнение — **constant-time** `hmac.compare_digest(provided, settings.admin_api_key)` (stdlib `hmac`, уже доступен — без новой зависимости);
  - провал/отсутствие заголовка → **`401`** RFC-7807 (`application/problem+json`, `type=.../unauthorized`), **без раскрытия** причины (как auth-провалы `current_user`).
- **Гейтинг пустого ключа (prod-безопасность):** если `ADMIN_API_KEY` пуст/не сконфигурирован (`None`/пустая строка) — `require_admin` **всегда `401`** (ни один `X-Admin-Key` не валиден; админ-плоскость де-факто отключена). Выбор `401` (а не `404`): один код-путь, нет ветвления роутера по конфигу, нет docs↔код-расхождения «эндпоинт то есть, то нет»; `compare_digest` против пустого ключа никогда не проходит. Эндпоинты видны в схеме (ADR-021 revision §C), но пустой ключ делает их недоступными (всегда `401`) — видимость в OpenAPI защиту не ослабляет.
- **Среда не гейтит:** админ-эндпоинты работают в **dev И prod** при заданном `ADMIN_API_KEY`. `settings.environment` в `require_admin` **не** участвует.

### B. Login-as — `POST /v1/admin/login-as`

- Защита — `require_admin`. Тело `{ "user_id": "u_...", "device_label": "string?" }`.
- Выпускает **свежий пользовательский Bearer** `lv_<key_id>_<secret>` за указанного `user_id` через существующий `token_service` (новая строка `api_tokens`, `device_label` по умолчанию `"admin-login"`). Ключ возвращается **один раз** (как `POST /auth/apple`).
- **Если `user_id` не существует** — создать `users` (минимальный upsert, как `/auth/apple`, но **без `apple_sub`**). `users.apple_sub` — уже `NULL UNIQUE` (раньше NULL допускался только для legacy S1 seed-юзера; ADR-021 расширяет: **admin-created юзеры тоже `apple_sub=NULL`**). `adapty_customer_user_id = users.id` (как при Apple-входе). Семантика upsert-полей — [auth §7](../modules/auth/03-architecture.md).
- Идентификатор юзера задаётся **клиентом-оператором** (тело `user_id`): если строка с таким `id` есть — выдать токен за неё; нет — создать с этим `id`. (Опуск `user_id` → сервер генерирует новый `u_...` и создаёт юзера — допустимо; нормативная форма — [auth §7](../modules/auth/03-architecture.md).)

### C. Подача админ-эндпоинтов в публичной OpenAPI — ВИДИМЫ под тегом «Администрирование» (ADR-021 revision)

> **Действующее решение (ADR-021 revision, 2026-06-17).** Зафиксировано в коде (`app/api/routers/admin.py`) и закреплено контракт-тестом (`tests/contract/test_admin_openapi_visible.py`).

- Все админ-эндпоинты (`POST /v1/admin/*`, `GET /v1/admin/*`) — **`include_in_schema=True`** (видимы в `/openapi.json` и `/docs`) под тегом **«Администрирование»**. Подача наружу — нормативный стандарт [api §B.4 (tag-таблица)](../modules/api/02-api-contracts.md#b4-группировка-по-доменам--tags-нормативный-перечень-русские-названия) и [§B.5](../modules/api/02-api-contracts.md#b5-скрытие-служебных--internal-эндпоинтов-из-публичной-схемы).
- **Security — per-operation `AdminKey`, НЕ глобальный `BearerAuth`.** Кастомный `app.openapi()` (`main.py`) объявляет схему `AdminKey` (`type: apiKey`, `in: header`, `name: X-Admin-Key`) в `components.securitySchemes` и навешивает `security=[{AdminKey: []}]` на каждую операцию `/v1/admin/*` (вместо наследуемого глобального `BearerAuth`). Так Swagger `Authorize` принимает админ-ключ. Защита `require_admin` (§A) действует **при любой видимости** — `include_in_schema` влияет только на присутствие в схеме, не на проверку ключа.
- **Денилист B.7 утечки внутренних маркеров остаётся обязательным:** docstring/`summary`/описания админ-эндпоинтов — **на русском, без `Sprint`/`ADR`/`TD`/имён агентов**. `admin`/`login-as`/`X-Admin-Key` теперь легитимны в схеме (публичная админ-поверхность), но внутренние процессные маркеры в `/openapi.json` запрещены ([api §B.7](../modules/api/02-api-contracts.md#b7-чек-лист-для-reviewerqa-grep-критерии-чистоты-openapijson)).

> **История (отозвано ADR-021 revision):** ~~Все админ-эндпоинты — `include_in_schema=False` (скрыты из `/openapi.json` и `/docs`), как `/metrics`/`/healthz` (B.5); скрытие — простейший способ гарантировать чистоту публичной схемы без отдельного тега «Администрирование».~~ Отозвано: админ-плоскость подаётся как явная публичная операторская поверхность под тегом «Администрирование» с собственной security-схемой `AdminKey`; чистота схемы обеспечивается денилистом B.7, а не скрытием.

### D. Бонус-генерации (кредиты) — ledger + денормализованный баланс

**Вариант (а) реализуется так:**
- **Append-only ledger `credit_grants`** (история начислений: `id`, `user_id`, `amount`, `reason?`, `idempotency_key?`, `created_by='admin'`, `created_at`) — аудит + идемпотентность начисления (UNIQUE по `(user_id, idempotency_key)` при заданном ключе).
- **Денормализованный баланс `users.bonus_generations_balance INT NOT NULL DEFAULT 0`** — O(1)-чтение на квота-гейте и в `GET /billing/me`. Инвариант: `balance == SUM(credit_grants.amount) - (списанные кредиты)`. Источник истины величины — баланс-колонка (атомарно мутируется); ledger — аудит-история начислений.
- **Знак `amount`:** начисление `amount > 0`. **Коррекция/списание отрицательным `amount` разрешена** (операторская правка ошибочного начисления), но итоговый `bonus_generations_balance` **не может стать < 0** (clamp на 0 / `409` при попытке увести в минус — нормативно [admin §3](../modules/admin/02-api-contracts.md)). Отрицательная коррекция тоже пишет строку ledger (аудит).

**Интеграция в quota-gate (`kind=generation`):**
- **Эффективный лимит** = `plan_quotas.monthly_generations` (за период) **+** `users.bonus_generations_balance`.
- **Порядок списания — сначала плановая месячная квота, затем кредиты** (чтобы кредиты НЕ сгорали раньше времени): пока `usage_counters.generations_used < monthly_generations` — инкрементируется `usage_counters.generations_used` (как сейчас); когда плановая квота исчерпана и есть кредиты — декрементируется `users.bonus_generations_balance`. Обе мутации — на **успешном старте generation-джобы**, идемпотентно по `job_id` (тот же guard, что `usage_counters` §5) — ровно одна из двух величин меняется на один старт.
- **Кредиты НЕ обнуляются помесячно** (в отличие от `usage_counters`, которые ключуются `period=YYYY-MM`): `users.bonus_generations_balance` — накопительный, переносится между периодами.
- **Гейт-проверка:** `generations_used < monthly_generations OR bonus_generations_balance > 0` → пропуск; иначе `402 reason=quota_exhausted`. Кредиты учитываются только для `kind=generation` (правки `kind=edit` гейтятся `monthly_edits`, кредиты их не покрывают — отдельная сущность).

**Админ-эндпоинты (require_admin):**
- `POST /v1/admin/users/{user_id}/credits` `{ amount, reason? }` — начислить/скорректировать. Идемпотентность — заголовок `Idempotency-Key` (опц.): повтор с тем же ключом не дублирует начисление (UNIQUE `credit_grants(user_id, idempotency_key)` → no-op, возврат текущего баланса). Без ключа — каждый вызов = новое начисление.
- `GET /v1/admin/users/{user_id}` — текущий баланс кредитов + квота юзера (`access_level`, остаток генераций/правок, `bonus_generations_balance`).

**Отражение в `GET /v1/billing/me`:** добавить `quota.bonus_generations_remaining` (= `users.bonus_generations_balance`) и учесть в `generations_remaining`: `generations_remaining = max(0, monthly_generations - generations_used) + bonus_generations_balance`.

## Consequences

- **(+)** Нет RBAC-фреймворка/ролей в БД — один секрет, одна dependency. Минимальная поверхность.
- **(+)** Админ-плоскость работает в dev и prod единообразно; пустой ключ безопасно отключает её.
- **(+)** Кредиты с аудит-ledger'ом и идемпотентностью; накопительный баланс не сгорает помесячно; плановая квота тратится первой.
- **(+)** Публичная Swagger чиста: админ виден отдельным тегом «Администрирование» с security `AdminKey` (ADR-021 revision §C), внутренние маркеры в схему не утекают (B.7 не нарушается).
- **(−)** `ADMIN_API_KEY` — общий секрет на всех операторов (нет per-operator аудита «кто начислил»). Достаточно для текущего масштаба; per-operator-аудит — отдельный ADR при необходимости.
- **(−)** Денормализованный баланс требует поддержания инварианта с ledger'ом (атомарная мутация в одной транзакции с ledger-insert). Покрывается тестом.
- **(−)** Login-as выпускает полноценный пользовательский токен — мощная операция; защищена только `ADMIN_API_KEY` (компрометация ключа = вход за любого юзера). Ключ — секрет уровня root-доступа, encrypted-at-rest, только в secret-manager/GitHub Secrets.

## Alternatives

- **RBAC-роли (`users.is_admin`, role-table):** отвергнуто — оверинжиниринг для одного оператора; добавляет миграцию роли + проверки в каждый эндпоинт. ADMIN_API_KEY достаточно.
- **Env-гейтинг админ-плоскости (только dev):** отвергнуто требованием — login-as и начисление кредитов нужны в prod (операторская выдача/поддержка). Безопасность — секрет, не среда.
- **`Authorization: AdminKey ...` вместо `X-Admin-Key`:** отвергнуто — смешивает с Bearer-парсингом `current_user`, усложняет middleware-порядок. Отдельный заголовок чище.
- **404 при пустом ключе (вместо 401):** отвергнуто — требует условного монтирования роутера по конфигу (docs↔код-расхождение «эндпоинт то есть, то нет»); `401`-always даёт единый код-путь независимо от видимости эндпоинтов в схеме (ADR-021 revision §C — эндпоинты видимы, но недоступны без валидного `X-Admin-Key`).
- **Только колонка `users.bonus_generations` без ledger:** отвергнуто — нет аудита начислений и слабее идемпотентность. Ledger + баланс даёт и аудит, и O(1)-чтение.
- **Списание кредитов первым (до плановой квоты):** отвергнуто — кредиты сгорали бы раньше плановой бесплатной квоты, что невыгодно юзеру и противоречит смыслу «бонус сверх квоты».
