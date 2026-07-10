# ADR-041 — Выравнивание field-extraction `apply_webhook_event` с фактической формой payload Adapty: конец несуществующих `profile.*`/`subscription.*`, инвариант «вебхук не понижает права по недостающим данным»

**Статус:** Accepted · **Дата:** 2026-07-10 · **Sprint:** 3.5 (прод-фикс денежного пути, второй дефект того же класса, что [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md))

Ревизует **извлечение подписочных полей** в приёмной части [ADR-027 §C/§D](ADR-027-adapty-webhook-bearer-token-grant.md) (`access_level`/`expires_at`/`will_renew`/`customer_user_id`) и нормативную таблицу переходов [billing §2.3](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица) в части **источника значений** этих полей. **Не пересматривает:** ключ дедупа и `KNOWN_EVENT_TYPES` ([ADR-040 §A/§B/§C](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) — в силе), авторизацию Bearer ([ADR-027 §A](ADR-027-adapty-webhook-bearer-token-grant.md)), always-200 ([§B](ADR-027-adapty-webhook-bearer-token-grant.md)), token-grant-модель и её значения (=0, [ADR-038](ADR-038-adapty-consumable-token-packs.md)), consumable-путь ([ADR-038](ADR-038-adapty-consumable-token-packs.md) — извлекает `vendor_product_id` из `event_properties`, уже корректно), identity-контракт ([ADR-027 §G](ADR-027-adapty-webhook-bearer-token-grant.md)), механику dual-source/grace ([ADR-004](ADR-004-adapty-source-of-truth.md)/[ADR-009](ADR-009-billing-idempotency-resync-grace.md)). **Уточняет** взаимодействие `synced_at`-бампа вебхука с resync-выборкой (§G — carve-out, чтобы бамп не подавлял resync-backstop, на который опираются §C/§D; согласованная политика `synced_at` для всех мутаторов `subscriptions`, cross-ref [Q-ADMIN-1](../99-open-questions.md#q-admin-1)) и наблюдаемость [ADR-040 §D](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) (регистрация метрики). Заводит [Q-BILLING-9](../99-open-questions.md#q-billing-9), [Q-BILLING-10](../99-open-questions.md#q-billing-10).

## Источник (сверено с first-party документацией провайдера)

Официальная документация Adapty **«Webhook event types and fields»** — <https://adapty.io/docs/webhook-event-types-and-fields>, **сверка выполнена 2026-07-10** (тот же первоисточник, что [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md); подтверждена дополнительно по разделу access-level-полей). Установленные факты о **фактическом расположении** полей, используемых при применении подписочного состояния:

1. **В payload вебхука Adapty НЕТ верхнеуровневого объекта `profile` и НЕТ верхнеуровневого объекта `subscription`.** Верхнеуровневые поля — плоский список (факт №2 [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)): `profile_id`, `customer_user_id`, `idfv`, `idfa`, `advertising_id`, `profile_install_datetime`, `user_agent`, `email`, `event_type`, `event_datetime`, `event_properties`, `event_api_version`, `profiles_sharing_access_level`, `attributions`, `user_attributes`, `integration_ids`, `play_store_purchase_token`. Никакого `payload["profile"]["access_level"]`, `payload["profile"]["expires_at"]`, `payload["profile"]["customer_user_id"]`, `payload["subscription"][...]` **не существует** — чтение по этим путям всегда даёт отсутствующее значение.

2. **`transaction_id`, `original_transaction_id`, `vendor_product_id`, `store`, `cancellation_reason`, `subscription_expires_at`, `profile_event_id`, `environment`, `profile_has_access_level`** — внутри `event_properties`.

3. **Срок подписки** для подписочных событий (`subscription_started`/`renewed`/`renewal_cancelled`/`expired`/`refunded`) — **`event_properties.subscription_expires_at`** (не `expires_at`, не `profile.expires_at`).

4. **Событие `access_level_updated`** несёт в `event_properties` собственный набор полей уровня доступа: **`access_level_id`, `is_active`, `is_in_grace_period`, `is_lifetime`, `is_refund`, `expires_at`, `renewed_at`, `starts_at`, `will_renew`, `activated_at`** и офферные поля. Именно здесь (и только здесь среди handled-событий) присутствуют `access_level_id`, булев `will_renew` и `expires_at` уровня доступа.

5. **Отмена автопродления** в подписочных событиях сигнализируется присутствием `event_properties.cancellation_reason`; булев `will_renew` per-событие гарантирован только в `access_level_updated` (факт №4). «Event properties can vary depending on the event type» ⇒ **ни `will_renew`, ни `expires_at`, ни `access_level_id` не гарантированы в каждом событии** (факт №3 [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)).

## Context

Прод-инцидент `nexoraweb.shop` (2026-07-10) вскрыл дефект извлечения **ключа дедупа** (починен [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)). Тот же корень — **контракт извлечения полей выведен по нашей внутренней схеме, а не по фактическому payload провайдера** — присутствует и во **втором**, латентном месте: `apply_webhook_event` (применение подписочного состояния) читает:

- `access_level` — из `payload["profile"]["access_level"]` (§2.3 «из `profile.access_level`»);
- `expires_at` — из `event_properties.expires_at || payload["profile"]["expires_at"]` ([ADR-027 §C](ADR-027-adapty-webhook-bearer-token-grant.md));
- `will_renew`/`expires_at` подписки — «из `subscription.*`» (§2.3);
- `customer_user_id` — с dead-fallback `profile.customer_user_id` ([ADR-027 §C](ADR-027-adapty-webhook-bearer-token-grant.md)).

По факту №1 объектов `profile`/`subscription` в payload **нет** ⇒ эти чтения всегда возвращают отсутствующее значение. **Последствие после выката [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md):** денежные события теперь **персистятся** и state-переходы отрабатывают, но подписочные поля наполняются из несуществующих ключей — `subscriptions.access_level`/`expires_at`/`will_renew` рискуют записаться пустыми/дефолтными. Consumable-путь ([ADR-038](ADR-038-adapty-consumable-token-packs.md)) корректен (читает `event_properties.vendor_product_id`), а **подписочный — нет**: денежный путь починен лишь частично. Дефект усугублён тем, что `getProfile`-resync — штатная страховка (§3) — на проде **не работает** (§D ниже): некорректное вебхук-состояние ничем не реконсилится.

Тот же самоподтверждающий-тест-эффект, что в [ADR-040 §F](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md): синтетические payload с объектом `profile` подтверждали неверную схему.

## Decision

### A. Нормативный контракт извлечения каждого подписочного поля (ревизует ADR-027 §C/§D, §2.3-источники)

`apply_webhook_event` извлекает поля **только** из фактических расположений (факты №1–5). Цепочки, обращающиеся к `profile.*`/`subscription.*`, **удаляются** как нерабочие.

| Наше поле | Фактическое расположение в payload | Правило извлечения |
|---|---|---|
| `event_type` | верхний уровень `event_type` | `.lower()` (без изменений) |
| `customer_user_id` | верхний уровень `customer_user_id` | как есть; **fallback `profile.customer_user_id` удаляется** (объекта нет). Legacy-fallback на верхнеуровневый `user_id` сохраняется как no-op-совместимость. Identity-контракт [ADR-027 §G](ADR-027-adapty-webhook-bearer-token-grant.md) не меняется |
| `vendor_product_id` | `event_properties.vendor_product_id` → `event_properties.product_id` | без изменений ([ADR-027 §C](ADR-027-adapty-webhook-bearer-token-grant.md), уже корректно) |
| `expires_at` (срок подписки) | `event_properties.subscription_expires_at` → `event_properties.expires_at` (для `access_level_updated`, факт №4) | первое непустое; **`profile.expires_at` удаляется**; отсутствует → **preserve** (§C) |
| `will_renew` | `event_properties.will_renew` (булев, факт №4) → иначе значение по семантике `event_type` из [§2.3](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица) (`started`/`renewed`→`true`; `renewal_cancelled`/`expired`/`refunded`→`false`; наличие `event_properties.cancellation_reason` ⇒ `false`) | отсутствует и семантикой не определён → **preserve** (§C) |
| `access_level` | **не присутствует в подписочных событиях**; для `access_level_updated` — `event_properties.access_level_id` (факт №4) | см. §B — не читается из `profile.*`; выводится по правилам §B |
| `is_active` / `is_in_grace_period` / `is_refund` | `event_properties.*` (только `access_level_updated`, факт №4) | вход для решения `status`/`access_level` в `access_level_updated` (§B) |
| `store` | `event_properties.store` | пишется в `subscriptions.store` (было `app_store`-константой); отсутствует → preserve/`app_store` |

Полный сырой payload по-прежнему сохраняется в `billing_events.payload` (jsonb) и `subscriptions.raw` — источник для ручного разбора/будущего расширения.

### B. `access_level` — приложение однотарифной модели, без чтения несуществующих ключей

Проект имеет **ровно один платный уровень**: `plan_quotas` ключуется `free`/`pro` ([billing §9](../modules/billing/03-architecture.md#9-сидинг-plan_quotas)). Отсюда нормативно:

- **`subscription_started` / `subscription_renewed` (в т.ч. в `grace`/`billing_issue`)** ⇒ `access_level='pro'` **устанавливается как константа платного тира**, без чтения из payload. Активная подписка ⇒ платный доступ по определению — та же модель, что `apply_admin_grant` ([§12.1](../modules/billing/03-architecture.md#121-apply_admin_grantsession--user_id-expires_at)) и `apply_storekit_subscription` ([§13.2](../modules/billing/03-architecture.md#132-начисление-и-идемпотентность)), которые тоже ставят `pro` напрямую. **Это ключевой фикс денежного пути:** для начисления pro `access_level_id` из payload не требуется.
- **`access_level_updated`** ⇒ решение по документированным булевам `event_properties` (факт №4), **не** по нашему предположению об имени уровня:
  - `is_active=true` ⇒ `access_level='pro'`, `status='grace'` если `is_in_grace_period=true`, иначе `status='active'`.
  - `is_refund=true` ⇒ `status='grace'`, `grace_until=now()+GRACE_PERIOD_DAYS` (как `subscription_refunded` §2.3); **`access_level` не понижается** (§C).
  - `is_active=false` (не refund) ⇒ `status` **не форсируется в `expired` на этом событии** (иначе — обход 7-дневного grace §6, продуктовое изменение); **`access_level` не затирается в `free`** (§C). Фактический teardown прав — по dedicated `subscription_expired`→`grace`→sweep (§6) и/или resync; access_level_updated с `is_active=false` фиксирует `will_renew`/`expires_at` (если присутствуют) и оставляет `status`/`access_level` под управлением lifecycle-события. Это согласовано с моделью «Adapty источник истины, teardown по grace» ([billing §6](../modules/billing/03-architecture.md#6-grace-период-сайтов-q-billing-1)) и не вводит новый переход.
  - Строка `access_level_id` для однотарифной модели **не требуется** (платный уровень один; `is_active` достаточно). Её точный маппинг понадобится только при вводе нескольких платных тиров — [Q-BILLING-9](../99-open-questions.md#q-billing-9) (не блокирует).

### C. Инвариант «вебхук не понижает права по недостающим данным» (preserve-on-missing)

Применение подписочных полей — **preserve-on-missing** (семантика COALESCE к существующей строке `subscriptions`):

- Поле **отсутствует** в payload ⇒ соответствующая колонка **сохраняет текущее значение** в БД; вебхук **никогда** не перезаписывает её пустым/дефолтным. В частности **запрещено** из-за отсутствия поля: понизить `access_level` `pro→free`, обнулить `expires_at→NULL`, форсировать `will_renew→false`.
- **Переходы `status`** (§2.3) остаются keyed на `event_type` (всегда присутствует) и применяются как прежде. Расчёт `grace_until` при `subscription_expired` без `subscription_expires_at`: fallback `now()+GRACE_PERIOD_DAYS` (как уже определено для `subscription_refunded` §2.3) — grace не теряется.
- Понижение прав возможно **только** по событию, чья семантика это предписывает (`subscription_expired`→`grace`→`expired` через sweep; `access_level_updated{is_active=false}`), и **только** через `status`/sweep-teardown, **не** через затирание `access_level` пустым значением из отсутствующего ключа.
- **Обоснование:** Adapty — источник истины, `subscriptions` — кэш (ADR-004). Ошибочно «обнулить» права из-за отсутствия поля в конкретном типе события (факт №5: `event_properties` варьируются) хуже, чем оставить прежнее корректное значение до следующего авторитетного события/resync. Инвариант делает частичный payload безопасным.

**Семантика preserve-on-missing на НОВОЙ строке (`_ensure_row`, [§12.1](../modules/billing/03-architecture.md#121-apply_admin_grantsession--user_id-expires_at)):** preserve — это COALESCE к *существующему* значению; на впервые создаваемой строке `subscriptions` «предыдущего значения» нет. Отсутствующее поле получает тогда **событийно-семантический дефолт** таблицы [§2.3](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица), а **не** инвентируемое значение:
- **`access_level`:** `subscription_started`/`subscription_renewed` ⇒ `pro` константой (§B) — опасный кейс «новый платящий = `free`» **закрыт**; `access_level_updated{is_active=true}` ⇒ `pro`; иное на новой строке ⇒ дефолт `free` (`_ensure_row`; прав к сохранению нет).
- **`will_renew`:** событийный дефолт §2.3 (`started`/`renewed` ⇒ `true`; `expired`/`refunded`/`renewal_cancelled` ⇒ `false`) — детерминирован, не «preserve в неизвестность». На новой строке для `started`/`renewed` при отсутствии `event_properties.will_renew` = `true`.
- **`expires_at`:** поле **отсутствует** и предыдущего значения нет ⇒ **`NULL`** — «pro без сохранённого срока», бессрочно-активно до следующего авторитетного события/resync. **Это осознанный исход, согласованный с [§12.2](../modules/billing/03-architecture.md#122-бессрочно-expires_atnull--без-ложного-истечения):** quota-gate (§4) для активной подписки `expires_at` **не читает**, `subscription_sweep` (§6) выбирает строго `status='grace' AND grace_until<now()` — активная строка с `expires_at=NULL` ложного истечения не получает (та же семантика, что бессрочный admin-grant §12.2). Это **over-grant, не понижение** — инвариант §C держится; `getProfile`-resync (при рабочем ключе, §D/[Q-BILLING-10](../99-open-questions.md#q-billing-10)) заполнит реальный `subscription_expires_at`. Инвентировать синтетический срок (`now()+N`) **отвергнуто:** угаданный неверный срок хуже безвредного `NULL`, не читаемого на активном гейте.

### D. Зависимость корректности от `getProfile`-resync (сейчас неработающего) — критично

Нормативная модель (§3, [ADR-004](ADR-004-adapty-source-of-truth.md)/[ADR-009](ADR-009-billing-idempotency-resync-grace.md)) полагает `getProfile`-resync **страховкой**, которая самокорректирует кэш при пропущенных/частичных вебхуках (в т.ч. `CONSCIOUSLY_IGNORED_EVENT_TYPES` [ADR-040 §C](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md)). **Установленный факт (прод, 2026-07-10):** `ADAPTY_API_KEY` на проде невалиден ⇒ `getProfile` отвечает `401` ⇒ и периодический (§3.1), и lazy (§3.2) resync работают в режиме **fail-open на кэш** — т.е. **НЕ реконсилят** состояние. Следствия:

1. **Вебхук — сейчас ЕДИНСТВЕННЫЙ рабочий источник истины подписочного состояния.** Корректность §A/§B/§C становится критической: страховки resync нет.
2. **Допущение [ADR-040 §C](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) «права по `CONSCIOUSLY_IGNORED` реконсилятся resync» на проде ВРЕМЕННО НЕВЕРНО** — до восстановления `ADAPTY_API_KEY` эти события не отражаются в правах вовсе. Это не меняет решение ADR-040 (осознанный no-op остаётся корректным дизайном при рабочем resync), но переводит восстановление resync в **операционный блокер prod-корректности** — [Q-BILLING-10](../99-open-questions.md#q-billing-10).
3. Инвариант §C дополнительно оправдан: без resync частичный/отсутствующий payload тем более нельзя трактовать как «понизить права».

Восстановление `ADAPTY_API_KEY`/здоровье resync — **операционная задача ops** (env/секрет прод-инстанса), фиксируется как [Q-BILLING-10](../99-open-questions.md#q-billing-10); не блокирует код-фикс §A–§C, но обязательна для полной prod-корректности. Наблюдаемость отставания resync — `lovable_adapty_resync_lag_seconds` ([observability §2.7](../modules/observability/03-architecture.md#27-billing--quota-billing)); при 401 lag растёт монотонно — сигнал ops.

### E. Наблюдаемость: регистрация метрики отброшенных/диагностируемых событий (уточняет ADR-040 §D)

[ADR-040 §D](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) требовал «счётчик отброшенных событий `{reason, event_type}`», но нормативного имени не давал, а в единственном нормативном доме метрик ([observability §2.7](../modules/observability/03-architecture.md#27-billing--quota-billing)) его не было — требование было **неисполнимо** (backend не изобретает имена). Нормативно фиксируется:

- **Метрика `lovable_billing_webhook_dropped_total`** (Counter, labels `reason`, `event_type`) регистрируется в [observability §2.7](../modules/observability/03-architecture.md#27-billing--quota-billing) — единый нормативный дом. `reason` ∈ `{profile_event_id_absent, unknown_event, unhandled_known_event, missing_customer_user_id, unknown_token_product}` (низкокардинальный enum), `event_type` — категория Adapty (~18). Покрывает **все** диагностические исходы вебхука [ADR-040 §D](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md), **включая** consumable `unknown_token_product` ([ADR-038 §E](ADR-038-adapty-consumable-token-packs.md)).
- **Ранее висящий alert `billing_unknown_token_product`** (упоминался в [ADR-038](ADR-038-adapty-consumable-token-packs.md)/[billing §11.3/§2.5](../modules/billing/03-architecture.md#113-consumable-token-паки-non_subscription_purchase-adr-038) как «существующий паттерн», но метрики в §2.7 не имел) приводится к согласованному виду: это **alert поверх `lovable_billing_webhook_dropped_total{reason="unknown_token_product"}`**, не отдельная метрика.
- **Значения payload в метрику не попадают** — только категориальные лейблы (согласовано с [05-security → политика логирования](../05-security.md#observability-как-security-сигнал)). Новой зависимости/env нет (`prometheus-client` в стеке).
- **Разграничение обязательности в текущем changeset:** обязателен диагностический **WARN/INFO-лог-след** ([ADR-040 §D](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md), уже реализован backend) — он, а не метрика, страхует денежный путь и **является условием деплоя** фикса. Инструментация метрики `lovable_billing_webhook_dropped_total` — **fast-follow** billing-наблюдаемости по теперь-определённому имени §2.7; **не блокирует деплой** field-extraction-фикса, но должна быть выполнена в том же спринте (при неработающем resync §D метрика/alert — единственный автоматический сигнал ops о дропах денежных событий). Cross-ref [ADR-040 §D](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) → §2.7 теперь указывает на **определённую** метрику, а не на требование изобрести имя.

### F. Testing-требование (усиливает ADR-040 §F)

Contract-тесты `apply_webhook_event` строятся от **официального образца payload Adapty** (факты №1–5): `access_level_id`/`is_active`/`will_renew`/`expires_at` — внутри `event_properties` (для `access_level_updated`), `subscription_expires_at` — внутри `event_properties` (для подписочных событий), **без** объектов `profile`/`subscription`. Обязательные сценарии — [06-testing-strategy → Adapty webhook](../06-testing-strategy.md#contract). Ключевой регресс-тест инцидента: payload **без** `profile`/`subscription` → `subscriptions.access_level`/`expires_at`/`will_renew` наполнены корректно (не пусто); частичный payload (без `expires_at`/`will_renew`) → существующие значения **сохранены** (инвариант §C), не обнулены.

### G. Взаимодействие `synced_at`-бампа вебхука с resync-backstop — carve-out против бессрочного подавления реконсиляции

Инвариант preserve-on-missing (§C) и §D явно опираются на `getProfile`-resync как страховку самокоррекции. Но `apply_webhook_event` бампит `subscriptions.synced_at=now()` на каждом handled-событии, а resync выбирает пользователей по протухшему `synced_at` — эти два механизма взаимодействуют и без ревизии оставляют пробел на денежном пути.

**Установленные факты (по фактическому коду, не по допущению):**
1. `apply_webhook_event` на **каждом** handled-событии ставит `subscriptions.synced_at=now()` (единая точка в конце функции `subscription_state.py`).
2. Периодический resync (`run_periodic_resync`, [billing §3.1](../modules/billing/03-architecture.md#31-периодический-celery-beat-billingresync)) выбирает строки `synced_at < now-TTL` **ИЛИ** `status ∈ {grace, billing_issue}`. **`synced_at`-бамп и есть механизм приоритета вебхука над resync** (формулировка §3.1 «вебхук с `received_at > synced_at` приоритетен» описывает именно это: приём вебхука обновляет `synced_at`). Отдельного сравнения по `billing_events.received_at` в resync-коде **нет** — `synced_at` load-bearing для упорядочивания.
3. `apply_admin_grant`/`apply_storekit_subscription` тоже ставят `synced_at=now()`, но с **другой** целью: защита grant'а юзера **без реального Adapty-профиля** от resync (getProfile вернул бы «нет активного профиля» → `expired`, затерев grant). Для них бамп корректен и намеренный ([§12.3](../modules/billing/03-architecture.md#123-сосуществование-с-adapty-ресинком--осознанное-следствие)/[§13.4](../modules/billing/03-architecture.md#134-сосуществование-с-adapty--без-двойного-начисления)/[Q-ADMIN-1](../99-open-questions.md#q-admin-1)).

**Пробел:** для `status=active` строк бамп на handled-событии выводит юзера из выборки resync на весь TTL; активно-подписанный юзер (регулярные renew/access_level_updated) может **никогда** не попасть в resync в пределах TTL, а следующий вебхук повторно бампит `synced_at` → подавление resync становится де-факто бессрочным. Опасен ровно один тип — **`access_level_updated{is_active=false}` (не refund)**: по §B он **преднамеренно НЕ делает teardown** (`status`/`access_level` — preserve, ждём lifecycle-`subscription_expired`), но бамп `synced_at` подавляет resync, который эту деактивацию и должен реконсилировать. Все прочие «понижающие» типы уводят `status` в `grace` (`expired`/`refunded`) или `billing_issue` — resync выбирает их **по статусу**, независимо от `synced_at` ⇒ пробела нет.

**Решение — развилка (b): вебхук ПРОДОЛЖАЕТ бампать `synced_at`, но с carve-out.**
- **Развилка (a) «вебхук не бампит `synced_at`» отвергнута:** по фактическому коду `synced_at` — единственный маркер приоритета вебхука над resync (факт 2); снятие бампа реинтродуцировало бы регрессию «resync затирает более свежее push-состояние вебхука» (лаг getProfile-снимка vs push). Предпосылка «упорядочивание уже на `received_at`» коду resync не соответствует.
- **Carve-out (нормативно):** `apply_webhook_event` на `access_level_updated` с `is_active=false` (не refund) **НЕ продвигает `synced_at`** (сохраняет прежнее значение) — событие не начисляет и не подтверждает активные права, поэтому не должно и «освежать» кэш. Следствие: строка остаётся resync-eligible (если уже протухла) либо протухает в пределах TTL → периодический/lazy resync сверяет истинное состояние Adapty (`is_active=false` в getProfile → `expired`; `is_active=true` → `active`). На **всех остальных** handled-событиях `synced_at=now()` сохраняется (приоритет вебхука над resync, факт 2).

**Согласованная политика `synced_at` для ВСЕХ мутаторов `subscriptions` в обход Adapty (единый нормативный источник):**

| Мутатор | `synced_at` | Причина |
|---|---|---|
| `apply_webhook_event`: подтверждающие события (`started`/`renewed`/`expired`/`refunded`/`billing_issue_detected`/`renewal_cancelled`/`access_level_updated{is_active=true}`/`access_level_updated{is_refund=true}`) | `now()` | вебхук = push от Adapty (истина); бамп даёт приоритет над resync |
| `apply_webhook_event`: `access_level_updated{is_active=false}` (не refund) | **НЕ продвигается** | не делает teardown (§B) — не должен подавлять resync-реконсиляцию (**carve-out**) |
| `apply_admin_grant` ([§12.1](../modules/billing/03-architecture.md#121-apply_admin_grantsession--user_id-expires_at)) | `now()` | **защита** grant'а без Adapty-профиля от resync-`expired` (§12.3/Q-ADMIN-1) |
| `apply_storekit_subscription` ([§13.2](../modules/billing/03-architecture.md#132-начисление-и-идемпотентность)) | `now()` | то же (§13.4/Q-ADMIN-1) |
| `apply_profile_resync` ([§3](../modules/billing/03-architecture.md#3-ресинк-getprofile)) | `now()` | это и есть resync |

Асимметрия (вебхук-carve-out vs admin/storekit-бамп) осознанна: admin/storekit — для юзеров **без** реального Adapty-профиля, где resync ошибочно снял бы grant → бамп защитный; вебхук — от самого Adapty, где resync есть желаемая страховка именно на `is_active=false`-подсказке, которую §B намеренно не применил.

**Проверяемость инварианта:** «вебхук/грант не понижает права по недостающим данным» (§C) держится без изменений — carve-out меняет только freshness-маркер, не entitlement-колонки. Дополнительно проверяемо: `access_level_updated{is_active=false}` **не** подавляет resync (строка остаётся/становится resync-eligible). Testing — §F/[06-testing → Adapty webhook](../06-testing-strategy.md#contract).

**Текущий прод-контекст (§D/[Q-BILLING-10](../99-open-questions.md#q-billing-10)):** resync на проде сейчас неработоспособен (`401`), поэтому carve-out фактически активируется **после** восстановления `ADAPTY_API_KEY`; до этого страховки нет ни при каком поведении `synced_at` (ops-блокер уже зафиксирован). Это **не меняет** выбор развилки — carve-out корректен как установившийся дизайн, а окно неработающего resync покрыто Q-BILLING-10.

**Наблюдаемость:** новой метрики/инструментации carve-out **не вводит** — отставание reconciliation покрывает существующая `lovable_adapty_resync_lag_seconds` ([observability §2.7](../modules/observability/03-architecture.md#27-billing--quota-billing)).

## Consequences

### Deploy-требование (нормативно): ADR-040 и ADR-041 — ОДНИМ changeset

Code-diff [ADR-040](ADR-040-adapty-webhook-dedup-key-event-type-reconciliation.md) (фикс ключа дедупа — реальные вебхуки начинают персиститься и **маршрутизироваться в `apply_webhook_event`**) и code-diff настоящего ADR-041 (field-extraction из фактических `event_properties` + preserve-on-missing + `access_level='pro'` для подписочных) выкатываются **ОДНИМ changeset**. **Раздельный выкат ADR-040 без ADR-041 ЗАПРЕЩЁН:** ADR-040 в одиночку направляет реальные вебхуки в `apply_webhook_event`, который **до** ADR-041 читает несуществующие `profile.*`/`subscription.*` ⇒ `subscription_started` оплатившего pro-юзера запишет `access_level='free'` — **активное понижение платящего пользователя** (денежный регресс, хуже статус-кво до ADR-040, где событие тихо терялось без записи). Обратный порядок (ADR-041 без ADR-040) безвреден, но бессмыслен (события до ADR-040 не персистятся). Требование обнаруживается devops/исполнителем здесь, в [billing §2](../modules/billing/03-architecture.md#2-webhook-handler-post-v1billingwebhookadapty) и в [billing README changelog](../modules/billing/README.md#changelog).

**Плюсы:** денежный путь починен полностью — подписочные поля берутся из фактических мест payload (сверено с first-party доке), pro начисляется на `subscription_started/renewed` без зависимости от отсутствующего `access_level_id`; инвариант preserve-on-missing исключает случайное понижение прав частичным/варьирующимся payload; carve-out §G закрывает пробел «`synced_at`-бамп вебхука подавляет resync-backstop» на единственном опасном событии (`access_level_updated{is_active=false}`), политика `synced_at` согласована для всех мутаторов `subscriptions`; определена семантика preserve-on-missing на новой строке (§C — `expires_at=NULL` осознанно, over-grant, не понижение); удалён самоподтверждающий-тест-класс (`profile`/`subscription` из фикстур); метрика диагностики отброшенных событий получила нормативное имя (исполнимое требование), заодно закрыт висящий `billing_unknown_token_product`; явно зафиксирована критическая зависимость от resync и его текущая неработоспособность (ops-блокер выведен в Q-BILLING-10). Без миграции (все колонки `subscriptions` существуют), без новой зависимости, без нового env.

**Минусы / осознанные риски:**
- **`access_level_updated` для однотарифной модели решается по `is_active`, а не по `access_level_id`** — при вводе нескольких платных тиров понадобится маппинг `access_level_id`→уровень ([Q-BILLING-9](../99-open-questions.md#q-billing-9)). Осознанно отложено (сейчас платный уровень один).
- **Инвариант preserve-on-missing может задержать понижение прав**, если Adapty пришлёт только «понижающее» событие без явных полей и без последующего `subscription_expired`/sweep — но при рабочем resync (§D) это самокорректируется; риск материален лишь в текущем окне неработающего resync ([Q-BILLING-10](../99-open-questions.md#q-billing-10)).
- **Метрика — fast-follow, не в деплой-гейте** — в окне до её инструментации ops опирается на WARN/INFO-логи (они обязательны и реализованы).

## Alternatives

- **Оставить извлечение из `profile.*`/`subscription.*` (статус-кво).** Отвергнута: объектов в payload нет (факт №1) → подписочные поля не наполняются; денежный путь чинится лишь частично.
- **Читать `access_level` из `event_properties.access_level_id` и для подписочных событий.** Отвергнута: `access_level_id` присутствует только в `access_level_updated` (факт №4), в `subscription_started/renewed` его нет; для однотарифной модели платный уровень выводится из самого факта активной подписки (§B) — надёжнее и не зависит от строки дашборда оператора.
- **Overwrite-on-missing (записывать `NULL`/`free`, если поле отсутствует).** Отвергнута: понижает права из-за варьирующихся `event_properties` (факт №5) — прямой риск ошибочного `pro→free`/сброса `expires_at`; при неработающем resync (§D) необратимо до ручного вмешательства.
- **Заблокировать фикс до восстановления `ADAPTY_API_KEY`/resync.** Отвергнута: field-extraction-фикс (§A–§C) самодостаточно корректен и снижает зависимость от resync; восстановление ключа — параллельная ops-задача ([Q-BILLING-10](../99-open-questions.md#q-billing-10)), не блокирует код денежного пути.
- **Отложить метрику §E в observability-спринт целиком.** Отвергнута: при неработающем resync (§D) метрика/alert — единственный автоматический сигнал ops о дропах денежных событий; имя регистрируется сейчас (исполнимость), инструментация — fast-follow того же спринта, не деплой-гейт.
