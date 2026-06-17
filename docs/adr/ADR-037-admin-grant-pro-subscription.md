# ADR-037 — Admin-эндпоинт выдачи pro-подписки выбранному юзеру

- **Статус:** Accepted
- **Дата:** 2026-06-17
- **Связанные:** [ADR-021](ADR-021-admin-plane-and-bonus-credits.md) (админ-плоскость, `require_admin`, кредиты), [ADR-004](ADR-004-adapty-source-of-truth.md) / [ADR-009](ADR-009-billing-idempotency-resync-grace.md) (Adapty source-of-truth, ресинк/grace), [ADR-027](ADR-027-adapty-webhook-bearer-token-grant.md) (приём вебхука, token-grant). **Не ревизует** ни один из них (см. §Consequences «Сосуществование»).

## Context

Оператору нужно выдать выбранному пользователю **pro-доступ** (`subscriptions.access_level=pro`), не симулируя вручную Adapty-вебхук `subscription_started`. Сейчас единственный путь поднять `access_level` до `pro` — реальная покупка через Adapty или ручная отправка поддельного вебхук-payload на `POST /v1/billing/webhook/adapty` (требует знания `ADAPTY_WEBHOOK_SECRET`, точной схемы payload и сопутствующих побочных эффектов — token-grant, `billing_events`).

Требования (нормативны):
1. **Срок pro — параметром запроса.** Оператор задаёт срок гибко: на N дней, до конкретной даты, либо **бессрочно**.
2. **Только pro-статус.** Эндпоинт ставит `access_level=pro` (квоты pro берутся из `plan_quotas`: 100 генераций/мес, безлимит проектов — [billing §9](../modules/billing/03-architecture.md#9-сидинг-plan_quotas)), но **НЕ начисляет токены** (`bonus_generations_balance` не трогается). Токены начисляются отдельно существующим `POST /v1/admin/users/{user_id}/credits` ([ADR-021 §D](ADR-021-admin-plane-and-bonus-credits.md)).

Ограничения, унаследованные от ADR-021: безопасность через секрет `ADMIN_API_KEY` (не RBAC), работа в dev И prod, единый стиль admin-эндпоинтов (роутер-префикс `/admin`, `require_admin`/`X-Admin-Key`, RFC-7807 ошибки, ответ-снимок как `AdminUserResponse`).

Существующая механика установки подписки сосредоточена в `app/billing/subscription_state.py` (`apply_webhook_event`, `_ensure_row`, `get_subscription`, статусы `STATUS_ACTIVE`/`GATE_PASS_STATUSES`). Прямой upsert `subscriptions` из admin-сервиса дублировал бы её и разошёлся бы со state-machine §2.3 — этого избегаем.

## Decision

### A. Контракт эндпоинта — `POST /v1/admin/users/{user_id}/subscription`

- **Путь и метод:** `POST /v1/admin/users/{user_id}/subscription`. Выбран по аналогии с существующим `POST /v1/admin/users/{user_id}/credits` ([admin §3](../modules/admin/02-api-contracts.md)) — sub-ресурс юзера под админ-роутером. Не отдельный top-level путь: операция логически принадлежит юзеру, как credits.
- **Auth:** `require_admin` (заголовок `X-Admin-Key`, constant-time против `ADMIN_API_KEY`) — тот же dependency и семантика, что у всех `/admin/*` ([ADR-021 §A](ADR-021-admin-plane-and-bonus-credits.md)). Пустой `ADMIN_API_KEY` → плоскость отключена (всегда `401`).
- **Публичная схема:** **`include_in_schema=True`** под тегом **«Администрирование»**, per-operation security `AdminKey` (`X-Admin-Key`) — как все `/admin/*` ([ADR-021 §C revision](ADR-021-admin-plane-and-bonus-credits.md), [api §B.4/§B.5](../modules/api/02-api-contracts.md#b4-группировка-по-доменам--tags-нормативный-перечень-русские-названия)); docstring/`summary` на русском без `Sprint`/`ADR`/`TD` (денилист B.7).
- **Body** (`AdminGrantSubscriptionRequest`): два **взаимоисключающих опциональных** поля срока —
  ```json
  { "duration_days": 30, "expires_at": null }
  ```
  - `duration_days: int | null` — срок в днях от `now()` (UTC). Должен быть `> 0`.
  - `expires_at: datetime | null` — явная дата окончания (ISO-8601, должна быть в будущем относительно `now()`).
  - **Оба `null` (или тело `{}`) ⇒ бессрочно** (`subscriptions.expires_at = NULL`, см. §C).
  - **Заданы оба одновременно ⇒ `422`** (`unprocessable`, `reason`-нейтральный detail) — неоднозначный срок.
  - `duration_days <= 0` или `expires_at` в прошлом/настоящем ⇒ `422`.
- **Ответ `200`** — `AdminUserResponse` (тот же снимок, что `GET /v1/admin/users/{user_id}`: `access_level`, `status`, `period`, `bonus_generations_balance`, `quota{...}`). Переиспользуется существующий `billing_service.build_billing_snapshot` + `admin_service.get_user` — оператор сразу видит результат (`access_level=pro`, обновлённые квоты). Отдельную subscription-only схему НЕ вводим: `AdminUserResponse` уже отражает `access_level`/`status` и пост-grant квоты.
- **Коды:** `200` (выдано); `401` (нет/неверный `X-Admin-Key`); `404` (юзера `user_id` нет — выдаём только существующему юзеру, в отличие от login-as, который создаёт; создание юзера здесь out-of-scope); `422` (оба поля срока заданы / невалидный срок / невалидное тело).

> **Почему `404`, а не авто-создание юзера:** grant pro имеет смысл только для существующего аккаунта (у несуществующего нет проектов/квот для применения). login-as создаёт юзера, потому что его задача — *выдать токен входа*; здесь же создание «pro-юзера из воздуха» — нежелательная семантика. Оператор сначала делает login-as/register, затем grant.

### B. Семантика установки pro — helper `apply_admin_grant` в `subscription_state`

Вводится **новый helper** `subscription_state.apply_admin_grant(session, *, user_id, expires_at) -> Subscription` по образцу `apply_webhook_event`. **НЕ** прямой upsert из `admin_service` (это дублировало бы state-machine §2.3 и разошлось бы при будущих правках маппинга). Helper:

1. `existing = await get_subscription(session, user_id)`; `sub = _ensure_row(session, user_id, existing)` — переиспользует существующую фабрику строки (одна строка на `user_id`, idempotent upsert).
2. Устанавливает поля:
   - `access_level = "pro"` (нормативное имя из `plan_quotas`, [billing §9](../modules/billing/03-architecture.md#9-сидинг-plan_quotas)).
   - `status = STATUS_ACTIVE` (`"active"`) — проходит quota-gate (`GATE_PASS_STATUSES`, [billing §4](../modules/billing/03-architecture.md#4-entitlements--quota-gate)).
   - `grace_until = None` — не под teardown (admin-grant не в grace).
   - `will_renew = False` — admin-grant **не** автопродляется; продление — повторный вызов эндпоинта. (Семантика: «выдан вручную на срок», не «подписка Apple».)
   - `expires_at` = вычисленное значение (§C): `now + duration_days`, либо переданный `expires_at`, либо `NULL` (бессрочно).
   - `started_at = now()` если ещё не задан (новая строка); существующий `started_at` (например от прежней реальной подписки) **сохраняется** — admin-grant не переписывает историю старта.
   - `synced_at = now()` — отметка свежести (приоритет над ресинком §3, как у вебхука) — **смягчает**, но не устраняет risk ресинка (§Consequences «Сосуществование»).
   - **Маркировка происхождения:** `store = "admin"`, `product_id = None`, `raw = {"source": "admin_grant", "granted_at": <iso>, "expires_at": <iso|null>}`. `store="admin"` отличает grant от реальной Adapty-строки (`app_store`) в аудите/диагностике. `adapty_transaction_id` не трогается.
3. Коммит — на стороне `admin_service` (одна транзакция). Токены (`bonus_generations_balance`, `credit_grants`, `billing_events`) **не создаются** — это нормативное отличие от вебхука (§требование 2, [billing §11](../modules/billing/03-architecture.md#11-token-grant-по-тиру-подписки-adr-027)).

`admin_service.grant_subscription(session, *, user_id, duration_days, expires_at)`: резолв юзера (`get_user` → `404` если нет), вычисление `expires_at`, вызов `apply_admin_grant`, `commit`, структурный аудит-лог (§F), возврат снимка.

### C. Срок «бессрочно» — `expires_at = NULL`, доказательство отсутствия ложного истечения

«Бессрочно» представляется `subscriptions.expires_at = NULL` (а **не** «далёкой датой» — это хрупкий хак и расходится с семантикой поля). Доказательство, что pro не истечёт ложно (по фактическому коду):

1. **quota-gate** ([billing §4](../modules/billing/03-architecture.md#4-entitlements--quota-gate), `quota_gate.py`/`entitlements.py`): пропуск определяется `status ∈ {active, grace}` (`gate_passes`). **`expires_at` на гейте не читается вообще.** `status=active` + `access_level=pro` ⇒ pro-доступ бессрочно.
2. **subscription_sweep** ([billing §6](../modules/billing/03-architecture.md#6-grace-период-сайтов-q-billing-1), `subscription_sweeper.py`): выбирает строго `status='grace' AND grace_until IS NOT NULL AND grace_until < now()`. Admin-grant имеет `status=active`, `grace_until=NULL` ⇒ **никогда не попадает в выборку sweep**. `expires_at` sweep тоже не консультирует.
3. **grace-переход** наступает только по вебхук-событиям `subscription_expired`/`subscription_refunded` (§2.3) — их для admin-grant нет (если у юзера нет реальной Adapty-подписки).

Следствие: `expires_at=NULL` + `status=active` = бессрочный pro, без участия `expires_at` в гейте/sweep. При **заданном** сроке (`duration_days`/`expires_at`) истечение pro **сейчас не автоматическое** — `expires_at` ни гейтом, ни sweep не проверяется (см. §Consequences «Срок не энфорсится автоматически» — осознанное следствие + [Q-ADMIN-1](../99-open-questions.md#q-admin-1)).

### D. Идемпотентность

`_ensure_row` гарантирует **одну строку `subscriptions` на `user_id`** (UNIQUE `subscriptions.user_id`, [03-data-model § subscriptions](../03-data-model.md#subscriptions-локальный-кэш-adapty)). Повторный вызов эндпоинта — **upsert**: обновляет ту же строку (новый `expires_at`/`synced_at`), не создаёт дубль. Это и есть механизм «продления» admin-grant. Дополнительного `Idempotency-Key` (как у credits) не вводим — операция natural-idempotent по `user_id` (повтор с тем же сроком = тот же результат; повтор с другим сроком = намеренное обновление).

### E. Без миграции, без env-ключей, без новых зависимостей

- **Схема БД не меняется:** переиспользуется существующая таблица `subscriptions` (все нужные поля — `access_level`, `status`, `will_renew`, `expires_at`, `started_at`, `grace_until`, `product_id`, `store`, `synced_at`, `raw` — уже есть, [03-data-model § subscriptions](../03-data-model.md#subscriptions-локальный-кэш-adapty), модель `Subscription`). **Миграция не требуется.**
- **Env-ключей нет.** Эндпоинт не вводит конфигурируемых параметров; срок — из тела запроса. (`ADMIN_API_KEY` — уже существует, ADR-021.)
- **Новых зависимостей нет** — stdlib `datetime` + существующий SQLAlchemy-слой.

### F. Audit

Структурный лог-ивент по образцу `admin_login_as`/`admin_grant_credits` (`admin_service.py`):
`logger.info("admin_grant_subscription", extra={"user_id": user_id, "access_level": "pro", "expires_at": <iso|null>, "duration_days": <int|null>})`. `ADMIN_API_KEY` в логах не печатается (конвенция [admin §Конвенции](../modules/admin/03-architecture.md#конвенции)).

## Consequences

- **(+)** Оператор выдаёт pro одним вызовом, без знания `ADAPTY_WEBHOOK_SECRET`/схемы payload и без побочных эффектов вебхука (token-grant, `billing_events`).
- **(+)** Нет дублирования механики подписки: `apply_admin_grant` переиспользует `_ensure_row`/статусы `subscription_state` — единый источник установки полей `subscriptions`.
- **(+)** Без миграции/env/зависимостей — минимальная поверхность; ответ-снимок переиспользует `AdminUserResponse`/`build_billing_snapshot`.
- **(+)** «Бессрочно» (`expires_at=NULL`) доказуемо безопасно: гейт/sweep не консультируют `expires_at` (§C).
- **(−) Сосуществование с реальной Adapty-подпиской и ресинком — главный риск.** `subscriptions` — кэш Adapty (source-of-truth — Adapty, ADR-004/009). Admin-grant пишет в тот же кэш. Если у юзера есть/появится реальный Adapty-профиль:
  - **Периодический `getProfile`-ресинк** (`resync.py`, `apply_profile_resync`) при протухшем `synced_at` тянет профиль. Если профиль **неактивен** (`is_active=false`) и `status` не в `{grace, billing_issue}` → `apply_profile_resync` ставит `status=expired` — **admin-grant сбрасывается в expired** (pro теряется). Если профиль **активен** — ресинк перезапишет `access_level` реальным значением.
  - **Реальный вебхук** (`subscription_expired`/`access_level_updated` и т.п.) аналогично перезапишет admin-grant по state-machine §2.3.
  - `synced_at=now()` (§B) откладывает периодический ресинк на один TTL-интервал (`BILLING_RESYNC_INTERVAL_S`), но **не** защищает от вебхука и от ресинка после протухания.
  - **Осознанное следствие:** admin-grant предназначен для юзеров **без активной реальной Adapty-подписки** (тестовые/операторские/комплиментарные аккаунты). Для юзера с живой Adapty-подпиской admin-grant — временный override до следующего ресинка/вебхука. Документировано; формализация политики (например, флаг «admin-pinned, ресинк не трогает») — [Q-ADMIN-1](../99-open-questions.md#q-admin-1).
- **(−) Срок (`duration_days`/`expires_at`) не энфорсится автоматически.** Ни гейт, ни sweep не сносят pro по наступлении `expires_at` (§C). `expires_at` для admin-grant сейчас — **информативное поле** (аудит/будущий sweep), не триггер истечения. Снятие pro по сроку = отдельная задача (расширить sweep на `access_level=pro AND store='admin' AND expires_at < now() → access_level=free/expired`) — [Q-ADMIN-1](../99-open-questions.md#q-admin-1). До её решения «срок» — операторская пометка; повторный вызов эндпоинта лишь **обновляет** срок (повтор = обновление той же строки `subscriptions`, §D), снять pro им нельзя (`expires_at` обязан быть в будущем — прошлое/настоящее → `422`, §A). Фактическое снятие pro по сроку до решения [Q-ADMIN-1](../99-open-questions.md#q-admin-1) = ручная правка БД либо будущий expires-sweep. **Не блокирует** базовое требование (выдать pro гибким сроком, включая бессрочно — выполнено).
- **(−)** `store="admin"`-маркер — конвенция, не enum-ограничение БД (`store` — `text NULL`); диагностика опирается на значение.

## Alternatives

- **Прямой upsert `subscriptions` из `admin_service`** (без helper в `subscription_state`): отвергнуто — дублирует state-machine §2.3, разойдётся при будущих правках маппинга событий; нарушает «единый источник установки полей подписки».
- **Переиспользовать `apply_webhook_event` с синтетическим `subscription_started`-payload:** отвергнуто — потянуло бы token-grant-ветку (§11) и потребовало бы фабриковать `profile`/`subscription_payload`/`event_id` + строку `billing_events`; смешивает admin-grant с вебхук-потоком. Узкий `apply_admin_grant` чище и не начисляет токены.
- **«Бессрочно» = далёкая дата** (`expires_at = 2999-…`): отвергнуто — хрупкий магический-литерал, расходится с семантикой `expires_at`, потенциально ломает будущий expires-based sweep. `NULL` — каноническое «нет даты окончания».
- **Авто-создание юзера при отсутствии (как login-as):** отвергнуто (§A) — grant pro несуществующему аккаунту бессмысленен; `404` яснее.
- **Отдельная subscription-only response-схема** вместо `AdminUserResponse`: отвергнуто — `AdminUserResponse` уже несёт `access_level`/`status`/квоты; новая схема — дублирование без выгоды.
- **Начислять токены вместе с pro:** отвергнуто требованием 2 — токены — отдельная ось (`/credits`), смешивать нельзя (иначе grant pro неявно раздаёт кредиты).
- **`will_renew=true` для admin-grant:** отвергнуто — нет автопродления (нет Adapty-транзакции); `true` вводил бы в заблуждение. Продление — повторный вызов эндпоинта.
