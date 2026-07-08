# ADR-039 — Прямой StoreKit 2 JWS-путь покупок (параллельно Adapty-вебхуку)

- **Статус:** Accepted
- **Дата:** 2026-07-08
- **Связанные:** [ADR-004](ADR-004-adapty-source-of-truth.md) / [ADR-009](ADR-009-billing-idempotency-resync-grace.md) (Adapty source-of-truth, ресинк/grace), [ADR-027](ADR-027-adapty-webhook-bearer-token-grant.md) (приём вебхука, token-grant, идемпотентность), [ADR-038](ADR-038-adapty-consumable-token-packs.md) (consumable-паки `non_subscription_purchase`, `TOKEN_PACK_PRODUCTS`, общий grant-примитив), [ADR-037](ADR-037-admin-grant-pro-subscription.md) (helper установки pro в `subscriptions`, сосуществование с ресинком), [ADR-007](ADR-007-sign-in-with-apple.md) (PyJWT[crypto] в стеке), [ADR-031](ADR-031-alembic-sync-engine-non-transactional-ddl.md) (движок миграций). **Дополняет** приёмную модель ADR-027/038 вторым каналом; **не ревизует** их семантику для Adapty-канала (см. §G «Сосуществование»).

## Context

Биллинг сейчас — **только** Adapty (server-to-server вебхук `POST /v1/billing/webhook/adapty`, [ADR-027](ADR-027-adapty-webhook-bearer-token-grant.md)/[ADR-038](ADR-038-adapty-consumable-token-packs.md)). Начисление токенов (`bonus_generations_balance` через `credit_grants`) и выдача pro (`subscriptions.access_level=pro`) происходят **только** когда Adapty присылает вебхук.

**Проблема.** При тестировании iOS-покупок через **Xcode StoreKit Testing** (локальный `.storekit`-конфигуратор, `environment="Xcode"`) транзакции подписываются **локальным тест-сертификатом Xcode** и **не доходят до серверов Apple/Adapty** → Adapty-вебхук **принципиально не приходит** → токены/подписка не начисляются, тестировать покупки невозможно. То же частично касается Sandbox (задержки/пропуски вебхуков).

**Референс-паттерн.** Соседний сервис (`claude-ios`) решил это **прямым StoreKit-путём**: приложение отправляет backend'у подписанную StoreKit 2 JWS-транзакцию, backend сам её криптографически верифицирует (x5c cert chain → доверенный Apple root, ES256-подпись) и начисляет. Работает во **всех** окружениях (Xcode + Sandbox + Production).

**Требование пользователя (нормативно).** Реализовать прямой StoreKit-путь **параллельно** существующему Adapty-вебхуку: два новых эндпоинта (токен-паки + подписка), собственный JWS-верификатор, переиспользование существующей grant-механики, идемпотентность, **безопасное сосуществование с Adapty без двойного начисления**, жёсткая per-instance безопасность (тест-сертификат Xcode — **не** на prod).

## Decision

### A. Новый JWS-верификатор — `app/billing/storekit.py`

Собственный верификатор Apple signed transaction (StoreKit 2 JWS), по образцу референса. Внешние зависимости — `cryptography` (x509 cert chain) + `PyJWT[crypto]` (ES256 JWS signature/claims); обе фиксируются в стеке (§F).

**Шаги верификации (fail-closed):**
1. **Разбор JWS.** Заголовок JWS (`alg=ES256`, `x5c`) → цепочка сертификатов `x5c` (base64 DER). `alg ≠ ES256` → отказ.
2. **Загрузка доверенных roots.** Каталог `APPSTORE_ROOT_CERT_DIR` (env, §E) → загрузка всех DER-сертификатов (`cryptography.x509.load_der_x509_certificate`). **Каталог пуст/не задан/не читается → нет доверенных roots → верификация невозможна → отказ (fail-closed, §C).**
3. **Верификация цепочки (`_verify_chain`).** Каждый сертификат цепочки `x5c` подписан следующим (подпись проверяется публичным ключом вышестоящего); цепочка **обязана терминироваться в одном из доверенных roots** из `APPSTORE_ROOT_CERT_DIR`. Не терминируется в доверенном root → отказ.
4. **Верификация подписи JWS.** Подпись самого JWS проверяется **публичным ключом leaf-сертификата** (первый в `x5c`), алгоритм **ES256** (`jwt.decode(..., algorithms=["ES256"], key=<leaf pubkey>)`). Невалидная подпись → отказ.
5. **Валидация payload.**
   - `bundleId == APPSTORE_BUNDLE_ID`, **если `APPSTORE_BUNDLE_ID` непусто**; **пустой `APPSTORE_BUNDLE_ID` ⇒ проверка bundle пропускается** (только тест/dev, §D/§E).
   - `environment` фиксируется (`Xcode` / `Sandbox` / `Production`) — попадает в `VerifiedTransaction`, для аудита/маршрутизации.
   - Отзыв (`revocationDate`/`revoked`) — фиксируется в `VerifiedTransaction.revoked`.
6. **Результат — `VerifiedTransaction` (frozen dataclass):** `transaction_id`, `original_transaction_id` (fallback на `transaction_id`), `product_id`, `expires_at` (опц., для подписок), `revoked: bool`, `environment`. **Сырой payload/JWS НЕ логируется** ([05-security → StoreKit](../05-security.md#прямой-storekit-jws-путь-adr-039)): в логи/Sentry идут максимум `transaction_id`/`environment`, никогда — тело транзакции или сам JWS.

Верификатор **чист от БД** (только крипто), возвращает `VerifiedTransaction` либо поднимает `StoreKitVerificationError` (роутер → `422`, §B).

### B. Эндпоинты — `POST /v1/tokens/purchase` и `POST /v1/subscription/sync`

Оба — **клиентские** (авторизация — пользовательский Bearer `token_service`, как весь клиентский API), тело `{ "jws": "<signed transaction>" }`. Нормативный контракт — [billing/02-api-contracts §4](../modules/billing/02-api-contracts.md#4-прямой-storekit-путь-adr-039).

**Начисление — на аутентифицированного `user_id` (Bearer), НЕ на `customer_user_id`/account из payload.** JWS доказывает валидную Apple-покупку; получатель начисления — **вызывающий** аккаунт. Кросс-аккаунтное переиспользование чужого валидного JWS (redeem за свой аккаунт) блокируется **глобальной** идемпотентностью по `transaction_id` (§D: транзакция начисляется ровно один раз в системе, за одного user_id).

- **`POST /v1/tokens/purchase`** — consumable токен-пак: `verify(jws)` → `VerifiedTransaction` → `resolve_consumable_tokens(product_id)` ([billing §11.3](../modules/billing/03-architecture.md#113-consumable-token-паки-non_subscription_purchase-adr-038)) → начисление через общий grant-примитив (§C).
- **`POST /v1/subscription/sync`** — подписка: `verify(jws)` → `VerifiedTransaction` → установка `access_level=pro`/`status=active`/`expires_at` из транзакции через helper `subscription_state` (§C).

**Коды/тела (мирроринг стиля вебхука `{status, reason?}`):**

| Условие | Код | Тело |
|---|---|---|
| нет/невалидный Bearer | `401` | RFC-7807 (без раскрытия) |
| невалидный JWS / цепочка не терминируется в доверенном root / ES256-подпись невалидна / bundle mismatch / **roots не сконфигурированы (fail-closed)** | `422` | RFC-7807 (`problem_type=invalid-storekit-transaction`, без раскрытия крипто-деталей) |
| tokens: неизвестный `product_id` (∉ `TOKEN_PACK_PRODUCTS`) | `200` | `{"status":"ignored","reason":"unknown_token_product"}` |
| tokens/subscription: транзакция `revoked` | `200` | `{"status":"ignored","reason":"revoked"}` |
| subscription: `expires_at` в прошлом | `200` | `{"status":"ignored","reason":"expired"}` |
| повтор той же `transaction_id` (idempotent replay) | `200` | `{"status":"duplicate"}` |
| tokens применено | `200` | `{"status":"applied","tokens_granted":N}` |
| subscription применено | `200` | `{"status":"applied","access_level":"pro","expires_at":<iso\|null>}` |
| реальный сбой БД | `5xx` | клиент повторит |

`422` — **fail-closed** для любой неверифицируемой транзакции, включая несконфигурированные roots (**не** начисляем на неверифицируемой транзакции — §C). Крипто-детали в теле не раскрываются.

### C. Начисление — переиспользование существующей механики (без дублирования write-path)

- **Токен-паки → общий grant-примитив ([ADR-038 §C](ADR-038-adapty-consumable-token-packs.md), [billing §11.2](../modules/billing/03-architecture.md#112-начисление-и-идемпотентность-adr-027-e)).** Маппинг `product_id → tokens` — **тот же** `resolve_consumable_tokens` поверх `TOKEN_PACK_PRODUCTS` (общий каталог для обоих каналов; отдельного env для StoreKit нет). Начисление — общий примитив `grant_tokens` (относительный атомарный `UPDATE users.bonus_generations_balance += amount` + insert `credit_grants`), **тот же**, что consumable-путь Adapty. Отличие вызова: `created_by='storekit'`, `reason='storekit:tokens_purchase'`, `idempotency_key='storekit:'+transaction_id`. **Обобщение примитива:** существующий `grant_tokens(session, *, user_id, event_id, event_type, amount)` расширяется параметрами `created_by`/`reason`/`idempotency_key` (или тонкий storekit-вызывающий поверх того же write-path); Adapty-вызовы дают **байт-в-байт** прежнее поведение (`created_by='adapty'`, `idempotency_key=event_id`) — write-path **не** дублируется.
- **Подписка → helper `subscription_state`.** По образцу [`apply_admin_grant`](ADR-037-admin-grant-pro-subscription.md) (§B ADR-037) — новый `apply_storekit_subscription(session, *, user_id, expires_at, environment, transaction_id, original_transaction_id)`: переиспользует `_ensure_row` (одна строка на `user_id`, idempotent upsert), ставит `access_level='pro'`, `status=STATUS_ACTIVE`, `grace_until=NULL`, `expires_at` из транзакции, `will_renew=false` (renewal приходит отдельной sync), `store='storekit'` (маркер происхождения, отличает от `app_store`/`admin`), `product_id` из транзакции, `adapty_transaction_id` не трогается, `synced_at=now()` (приоритет над ресинком на один TTL — §G), `raw={source:'storekit', environment, transaction_id, ...}`. **Токены НЕ начисляет** (подписка = только pro, как ADR-037/ADR-038 §D). НЕ прямой upsert из роутера — единый источник установки `subscriptions` ([billing §2.3/§12](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица)).

### D. Идемпотентность — глобальная по `transaction_id`, новая таблица `store_transactions`

Токен-начисление — **инкремент**, обязан быть строго идемпотентен, причём **глобально** (не per-user): одна Apple-транзакция начисляется **ровно один раз во всей системе, ровно одному `user_id`** — иначе переигранный (leaked/shared) чужой JWS начислился бы нескольким аккаунтам (`credit_grants` UNIQUE — `(user_id, idempotency_key)`, **per-user**, кросс-аккаунтную переигровку НЕ ловит).

**Решение — новая таблица `store_transactions` с `transaction_id` как PK** ([03-data-model → store_transactions](../03-data-model.md#store_transactions-прямой-storekit-путь-adr-039)):

| Поле | Тип | Заметки |
|---|---|---|
| `transaction_id` | text **PK** | Apple `transactionId` — **глобально уникален**. PK ⇒ транзакция редимится один раз (любым user_id). |
| `original_transaction_id` | text NULL | Для подписок/renewal-цепочек. |
| `user_id` | text FK→users NOT NULL | Аккаунт, которому начислено (Bearer-вызывающий). |
| `product_id` | text | SKU из транзакции. |
| `kind` | text | `tokens_purchase` / `subscription_sync`. |
| `environment` | text | `Xcode` / `Sandbox` / `Production`. |
| `amount` | int NULL | Начисленные токены (для `tokens_purchase`). |
| `created_at` | timestamptz NOT NULL | |

- **Token purchase:** в **одной транзакции** — insert `store_transactions(transaction_id, kind='tokens_purchase', ...)` + `grant_tokens` (credit_grants + balance delta). Конфликт PK (`transaction_id` уже есть) → `200 duplicate`, начисление **не** повторяется (глобально; в т.ч. попытка другого `user_id` переиграть → duplicate, повторного начисления нет).
- **Subscription sync:** insert `store_transactions(kind='subscription_sync')` `ON CONFLICT (transaction_id) DO NOTHING` + `apply_storekit_subscription` (state-set, natural-idempotent). **Renewal** = новая `transaction_id` (тот же `original_transaction_id`) → новая строка → обновление `subscriptions.expires_at`. Повтор той же `transaction_id` → `200 duplicate` (подписка уже отражает это состояние).

**Миграция — транзакционный `create_table`** ([ADR-031](ADR-031-alembic-sync-engine-non-transactional-ddl.md): обычный `op.create_table`, БЕЗ `autocommit_block` — нет enum/`ADD VALUE`; движок sync psycopg по `DATABASE_URL_SYNC`), revises `20260617_0001`. Без backfill.

### E. Env-контракт и провизия сертификатов

Два новых ключа (потребитель — **api**: эндпоинты и верификатор исполняются в FastAPI-процессе). Нормативно — [07-deployment → StoreKit](../07-deployment.md#прямой-storekit-путь-adr-039).

- **`APPSTORE_ROOT_CERT_DIR`** (поле `appstore_root_cert_dir: str`, api) — каталог доверенных root-сертификатов (DER `.cer`). Дефолт — **фиксированное местоположение в образе** `certs/appstore`. Пуст/не существует/без сертификатов → верификатор поднимает ошибку → **`422` fail-closed** (§C). Провизия — **devops** (mount/bake каталога). **Apple production root** (`AppleRootCA-G3.cer`) — публичный сертификат, **коммитится в git** (`certs/appstore/`). 
- **`APPSTORE_BUNDLE_ID`** (поле `appstore_bundle_id: str`, api) — ожидаемый `bundleId` iOS-приложения. **Пусто ⇒ проверка bundle пропускается** (Xcode/тест). Prod — реальный bundle id (`mba.gipsy.lovable`, как `APNS_BUNDLE_ID`).
- **Xcode StoreKit-Testing сертификат** (`StoreKitTestCertificate.cer`) — публичный тест-сертификат Apple; хранится в репозитории **отдельно** от prod-каталога (напр. `certs/appstore-xcode/`), **никогда** не попадает в `APPSTORE_ROOT_CERT_DIR` prod-инстанса (§D безопасности ниже).

### F. Зависимости и стек

- **`cryptography`** — используется напрямую (`x509` cert-chain + `ec`/`rsa` public-key verify в `app/billing/storekit.py`). Сейчас — **транзитивная** через `PyJWT[crypto]`. По правилу «прямое использование — прямая зависимость» ([02-tech-stack](../02-tech-stack.md#безопасность-библиотеки)) объявляется **явной прямой** зависимостью (`pyproject.toml` + стек). Версия — `>=43` (совместима с `PyJWT[crypto]`; референс-паттерн зафиксирован на `43.x`; точный pin — devops).
- **`PyJWT[crypto]`** — **уже** в стеке ([ADR-007](ADR-007-sign-in-with-apple.md): Apple RS256; extra `crypto` покрывает и ES256). Переиспользуется для ES256-верификации JWS. **Новой** библиотеки не вводит.
- **Adapty SDK не вводится** (как и прежде — [02-tech-stack §Биллинг](../02-tech-stack.md#биллинг)).

### G. Сосуществование с Adapty — без двойного начисления

Оба канала пишут в **те же** `bonus_generations_balance`/`credit_grants`/`subscriptions`. Идемпотентность у них — по **разным** ключам: Adapty — `billing_events.adapty_event_id` (= `event_id`); StoreKit — `store_transactions.transaction_id`. **В пределах одного канала** двойного начисления нет; **между каналами** (одна покупка пришла И в Adapty→вебхук, И напрямую) ключи разные → потенциальное двойное начисление.

**Нормативная семантика разграничения каналов (primary):**
- **`environment="Xcode"` → ТОЛЬКО прямой StoreKit-путь.** Adapty структурно не может доставить вебхук (транзакция не покидает устройство) — пересечения нет by construction.
- **`environment ∈ {Sandbox, Production}` → Adapty-вебхук — авторитетный канал** нормального потока покупок. iOS-клиент **НЕ** обязан дублировать ту же покупку в прямой эндпоинт. Прямые эндпоинты **остаются функциональны** в Sandbox/Prod (QA/фолбэк при недоступности вебхука), но **не** часть штатного потока — клиент не шлёт одну покупку в оба канала.

**Defense-in-depth (hardening, отложено — [Q-BILLING-7](../99-open-questions.md#q-billing-7)):** сделать кросс-канальный дедуп структурным — оба канала консультируют **общий** ключ по Apple `transaction_id` (напр. Adapty consumable-путь проверяет/пишет `store_transactions.transaction_id`, когда payload Adapty несёт Apple `transaction_id`). Активация требует верификации равенства «Adapty `transaction_id` == StoreKit `transactionId`» по реальным payload'ам Adapty — **пока не подтверждено**, Adapty-канал не трогаем (нулевая регрессия ADR-027/038), кросс-канальный guard — контракт разграничения выше. Не блокирует (прямой путь самодостаточно идемпотентен глобально §D; основной кейс — Xcode — пересечения не имеет).

**Подписка (`/subscription/sync`) и ресинк — то же осознанное следствие, что admin-grant ([ADR-037 §C/Consequences](ADR-037-admin-grant-pro-subscription.md), [billing §12.3](../modules/billing/03-architecture.md#123-сосуществование-с-adapty-ресинком--осознанное-следствие)):** `subscriptions` — кэш Adapty (source-of-truth — Adapty). StoreKit-sync пишет в тот же кэш (`store='storekit'`, `synced_at=now()`); при наличии **реального** Adapty-профиля периодический `getProfile`-ресинк/вебхук может перезаписать grant. Для Xcode-тест-юзеров реального Adapty-профиля **нет** → на тест/dev-инстансах, где ведётся StoreKit-тестирование, ресинк по этим юзерам либо не даёт активного профиля, либо не сконфигурирован; StoreKit-sync — авторитет. `synced_at=now()` откладывает периодический ресинк на один TTL. Формализация pin-семантики — общая с [Q-ADMIN-1](../99-open-questions.md#q-admin-1)/[Q-BILLING-7](../99-open-questions.md#q-billing-7).

### H. Безопасность (жёстко — §D раздела 05-security)

Единый нормативный источник — [05-security → Прямой StoreKit JWS-путь](../05-security.md#прямой-storekit-jws-путь-adr-039). Кратко:
- **Тест-сертификат Xcode в доверенных roots + пустой `APPSTORE_BUNDLE_ID` разрешены ТОЛЬКО на тест/dev-инстансах.** На **prod** это дыра (кто угодно self-sign'ит Xcode-JWS локальным тест-сертификатом → бесплатные токены/pro). **Prod-инстансы: ТОЛЬКО Apple production root в `APPSTORE_ROOT_CERT_DIR` + реальный `APPSTORE_BUNDLE_ID`.** Энфорс — **per-instance env + набор cert-файлов** в каталоге (devops): prod-каталог содержит только `AppleRootCA-G3.cer`; `StoreKitTestCertificate.cer` — только в cert-каталогах тест/dev-инстансов.
- **Prod безопасен на Apple root:** JWS с реальной Apple-подписью криптографически неподделываем (нельзя выпустить leaf, терминирующийся в Apple production root).
- **fail-closed:** roots не сконфигурированы → `422`, начисление **не** производится (§C).
- **Кросс-аккаунтная переигровка** чужого валидного JWS блокируется глобальной идемпотентностью `store_transactions.transaction_id` (§D).
- **Payload/JWS не логируются** (§A) — как identity-токены Apple/секреты в scrubbing ([05-security → Observability](../05-security.md#observability-как-security-сигнал)).

## Consequences

- **(+)** StoreKit-покупки тестируемы через Xcode StoreKit Testing (где Adapty-вебхук структурно недоступен); тот же путь покрывает Sandbox/Production.
- **(+)** Переиспользование существующей grant-механики: `resolve_consumable_tokens`/`grant_tokens` (токены), `_ensure_row`/`subscription_state` (подписка) — write-path не дублируется; общий каталог `TOKEN_PACK_PRODUCTS`.
- **(+)** Глобальная идемпотентность `store_transactions.transaction_id` — самодостаточна в пределах канала, блокирует кросс-аккаунтную переигровку.
- **(+)** Безопасность prod доказуема: только Apple root + реальный bundle id; JWS неподделываем; fail-closed без roots; тест-сертификат гейтится per-instance.
- **(−) Кросс-канальное двойное начисление** (одна покупка в Adapty И напрямую в Sandbox/Prod) не устранено структурно — держится на контракте разграничения каналов (§G); структурный дедуп — [Q-BILLING-7](../99-open-questions.md#q-billing-7) (отложено до верификации равенства transaction_id). Для основного кейса (Xcode) пересечения нет.
- **(−) Подписочный sync в тот же кэш `subscriptions`** — перезаписываем реальным Adapty-ресинком/вебхуком (то же следствие, что admin-grant §12.3); предназначен для юзеров без активной реальной Adapty-подписки (Xcode-тест). Срок (`expires_at`) энфорса истечения pro — общий gap с [Q-ADMIN-1](../99-open-questions.md#q-admin-1) (гейт `expires_at` не читает).
- **(−)** Новая таблица `store_transactions` (+транзакционная миграция) и явная зависимость `cryptography` — минимальная поверхность, обоснованы корректностью (глобальный дедуп) и правилом прямых зависимостей.
- **(−)** `store='storekit'` — конвенция-маркер (`store` — `text NULL`), не enum-ограничение.

## Alternatives

- **Дедуп токенов на `credit_grants(user_id, idempotency_key)` (per-user), без `store_transactions`:** отвергнуто — per-user UNIQUE не ловит кросс-аккаунтную переигровку чужого JWS (та же транзакция начислилась бы разным user_id). Глобальный `transaction_id`-PK обязателен.
- **Репаза `billing_events.adapty_event_id` под `storekit:{transaction_id}` для глобального дедупа (без миграции):** отвергнуто — семантически колонка Adapty-специфична (`adapty_event_id`, `payload=сырой вебхук`); засорение конфликтует с запросами/предположениями Adapty-ledger. Выделенная таблица честнее.
- **Немедленно унифицировать идемпотентность обоих каналов на `transaction_id` (ревизия ADR-038 §C сейчас):** отвергнуто в этой итерации — равенство «Adapty `transaction_id` == StoreKit `transactionId`» не верифицировано по реальным payload'ам; правка работающего Adapty-канала на непроверенном допущении рискованна. Отложено в [Q-BILLING-7](../99-open-questions.md#q-billing-7) с безопасным дефолтом (контракт разграничения каналов).
- **Только environment-разграничение без прямого верификатора (StoreKit-путь = Adapty для всех сред):** отвергнуто — не решает Xcode (вебхук не приходит вообще).
- **Отдельный env-каталог токен-паков для StoreKit:** отвергнуто — `product_id` те же App Store SKU; единый `TOKEN_PACK_PRODUCTS` (общий каталог) избегает рассинхрона маппинга между каналами.
- **Начислять на `customer_user_id` из payload (как Adapty):** отвергнуто — прямой путь Bearer-аутентифицирован; начисление на вызывающего + глобальный дедуп корректнее и проще (payload StoreKit не содержит нашего `user_id`).
- **Adapty Server-side receipt-validation API вместо собственного JWS-верификатора:** отвергнуто — не покрывает Xcode (транзакция не доходит до Apple/Adapty); собственная верификация x5c→Apple root работает офлайн для всех сред.
