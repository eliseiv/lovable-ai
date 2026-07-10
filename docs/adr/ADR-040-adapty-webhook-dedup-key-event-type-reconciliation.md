# ADR-040 — Ревизия контракта Adapty webhook: ключ дедупликации `profile_event_id` (внутри `event_properties`), запрет тихого дропа денежного события, согласование `KNOWN_EVENT_TYPES` с фактическим перечнем провайдера

**Статус:** Accepted · **Дата:** 2026-07-10 · **Sprint:** 3.5 (прод-фикс денежного пути)

Ревизует **приёмную часть** [ADR-027 §C](ADR-027-adapty-webhook-bearer-token-grant.md) (дефенсивное извлечение идентификатора события) и **§F** (имя события отмены), **§B** (строка таблицы `missing_event_id`). **Не пересматривает** авторизацию (§A Bearer), always-200-политику как принцип (§B), token-grant-модель (§D/§E), identity-контракт (§G), consumable-путь ([ADR-038](ADR-038-adapty-consumable-token-packs.md)), dual-source/resync/grace ([ADR-004](ADR-004-adapty-source-of-truth.md)/[ADR-009](ADR-009-billing-idempotency-resync-grace.md)). Закрывает [Q-BILLING-5](../99-open-questions.md#q-billing-5).

## Источник (сверено с first-party документацией провайдера)

Официальная документация Adapty **«Webhook event types and fields»** — <https://adapty.io/docs/webhook-event-types-and-fields>, **сверка выполнена 2026-07-10**. Установленные факты, на которых основана ревизия:

1. **В payload вебхука Adapty НЕТ верхнеуровневых `event_id` / `id`.** Уникальный идентификатор события для дедупликации — **`profile_event_id`** (тип **UUID**), расположен **ВНУТРИ объекта `event_properties`**, не на верхнем уровне.
2. **Верхнеуровневые поля payload:** `profile_id`, `customer_user_id`, `idfv`, `idfa`, `advertising_id`, `profile_install_datetime`, `user_agent`, `email`, `event_type`, `event_datetime`, `event_properties`, `event_api_version`, `profiles_sharing_access_level`, `attributions`, `user_attributes`, `integration_ids`, `play_store_purchase_token`.
3. **`event_properties` варьируются по типу события** («Event properties can vary depending on the event type and even between events of the same type») ⇒ `profile_event_id` **не гарантированно присутствует** для каждого `event_type` (в частности — служебные события вроде `access_level_updated`).
4. **Рекомендация провайдера по дедупу/порядку:** «Don't rely on `event_datetime` to order events. Instead, order events by your own receipt time, and deduplicate them using `profile_event_id` or the transaction IDs.» — т.е. допустимые ключи дедупа: `profile_event_id` **или** transaction-идентификаторы; упорядочивание — по **нашему времени приёма**, не по `event_datetime`.
5. **Полный фактический перечень `event_type` (18):** `subscription_started`, `subscription_renewed`, `subscription_renewal_cancelled`, `subscription_renewal_reactivated`, `subscription_expired`, `subscription_paused`, `subscription_deferred`, `non_subscription_purchase`, `trial_started`, `trial_converted`, `trial_renewal_cancelled`, `trial_renewal_reactivated`, `trial_expired`, `entered_grace_period`, `billing_issue_detected`, `subscription_refunded`, `non_subscription_purchase_refunded`, `access_level_updated`.
6. **Значения `subscription_cancelled` у Adapty НЕ существует.** Реальное имя события отмены автопродления — **`subscription_renewal_cancelled`**.

## Context

Прод-инцидент (`nexoraweb.shop`, 2026-07-10, денежный путь): три реальных вебхука Adapty на `POST /v1/billing/webhook/adapty` вернули HTTP 200, но **не обработались** и **не попали в `billing_events`** — начисления не произошли. Adapty считает доставку успешной (200) и **не ретраит**.

Root cause: [ADR-027 §C](ADR-027-adapty-webhook-bearer-token-grant.md) нормативно фиксировал извлечение идентификатора как `event_id || id`. На **реальном** payload (факт №1) оба поля отсутствуют ⇒ извлечение всегда даёт `None` ⇒ срабатывает ветка [ADR-027 §B](ADR-027-adapty-webhook-bearer-token-grant.md) `200 {"status":"ignored","reason":"missing_event_id"}` — **тихий дроп денежного события с success-кодом**, без строки в `billing_events`, без диагностического следа. Контракт §C был выведен **без сверки с документацией провайдера** и не проверялся на реальном payload — все E2E строились на синтетических payload'ах по нашей же (ошибочной) схеме и были **самоподтверждающими** (см. §Testing-требование).

Сопутствующе: `KNOWN_EVENT_TYPES`/[ADR-027 §F](ADR-027-adapty-webhook-bearer-token-grant.md)/[§2.3](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица) используют несуществующее имя `subscription_cancelled` (факт №6, отложенный [Q-BILLING-5](../99-open-questions.md#q-billing-5)), а список известных типов не согласован с фактическим перечнем провайдера (факт №5).

## Decision

### A. Ключ дедупликации — `profile_event_id` внутри `event_properties` (ревизует ADR-027 §C)

Нормативное извлечение ключа идемпотентности события (единственная точка дедупа — `billing_events.adapty_event_id`, UNIQUE), **резолюция по приоритету, первый непустой**:

1. **`event_properties.profile_event_id`** — канонический ключ провайдера (UUID, факт №1). Сохраняется в `billing_events.adapty_event_id` **как есть** (значение UUID).
2. **Transaction-идентификатор** (endorsed провайдером, факт №4), когда `profile_event_id` отсутствует: синтетический ключ `adapty-syn:{event_type}:{txid}`, где `txid = event_properties.transaction_id || event_properties.original_transaction_id`. Скоупинг префиксом `event_type` исключает кросс-типовую коллизию одной транзакции (один `transaction_id` может фигурировать в нескольких событиях — `subscription_started` + `access_level_updated`).
3. **Хэш сырого тела** (последний резерв, гарантия «никогда не тихо не дропнуть»): `adapty-syn:body:{sha256(raw_body_bytes)}` (`hashlib`, stdlib — **без новой зависимости**). Детерминирован ⇒ идентичная переотправка того же payload → тот же ключ → корректный дедуп; различные события → различный хэш → обрабатываются.

**Извлечение `event_type` и прочих полей** остаётся по [ADR-027 §C](ADR-027-adapty-webhook-bearer-token-grant.md) с уточнением фактического расположения (все — `event_properties.*` либо верхний уровень, факт №2): `event_type` (верхний уровень) → `.lower()`; `customer_user_id` (верхний уровень) → identity-контракт [ADR-027 §G](ADR-027-adapty-webhook-bearer-token-grant.md); `vendor_product_id`, `expires_at`, `transaction_id`, `profile_event_id` — внутри `event_properties`. Цепочка `event_id || id` **удаляется** как нерабочая.

> **Извлечение подписочных полей `apply_webhook_event` (`access_level`/`expires_at`/`will_renew`) ревизовано [ADR-041 §A](ADR-041-adapty-webhook-field-extraction-real-payload.md) (тот же инцидент, второй дефект):** данный ADR починил только **ключ дедупа**; поля состояния подписки в `apply_webhook_event` читались из несуществующих `profile.*`/`subscription.*` — фактические места (`event_properties.subscription_expires_at`/`.will_renew`/`.is_active`; `access_level`=`pro` для подписочных событий) и инвариант «вебхук не понижает права по недостающим данным» — в [ADR-041](ADR-041-adapty-webhook-field-extraction-real-payload.md).

**Упорядочивание событий** — по **времени приёма** (`billing_events.received_at`), не по `event_datetime` (факт №4). Это уже реализовано в resync (§3.1: «вебхук с `received_at > synced_at` приоритетен») — ревизия лишь фиксирует согласованность с рекомендацией провайдера; поведение resync не меняется.

### B. Запрет тихого дропа денежного события — ключ дедупа выводится ВСЕГДА (ревизует ADR-027 §B)

Резолюция §A **всегда** возвращает непустой ключ (тир 3 — хэш тела — не может дать пусто на непустом теле). Следствие: строка **`missing_event_id`** таблицы кодов [ADR-027 §B](ADR-027-adapty-webhook-bearer-token-grant.md) **упраздняется** — событие с отсутствующим `profile_event_id` больше **НЕ** отбрасывается, а обрабатывается по синтетическому ключу и **персистится в `billing_events`** (что было невозможно при `None`-ключе на `adapty_event_id text NOT NULL`).

**Наблюдаемость fallback (требование §D):** срабатывание тира 2 или 3 (т.е. `profile_event_id` **отсутствовал**) обязано оставлять **диагностический WARN-след** (`reason=profile_event_id_absent`) — это сигнал рассинхрона со схемой провайдера, требующий внимания, а не штатный путь.

Ветки always-200 для **действительно** невалидного ввода (`empty_body`/`invalid_json`/`not_an_object`) и `missing_customer_user_id` — **без изменений** (§B ADR-027). Принцип «5xx только на реальный сбой БД» сохраняется.

### C. Согласование `KNOWN_EVENT_TYPES` с фактическим перечнем провайдера (закрывает Q-BILLING-5)

`subscription_cancelled` → **`subscription_renewal_cancelled`** (факт №6) во всех нормативных точках (§2.3, ADR-027 §F, `KNOWN_EVENT_TYPES`, data-model, tests). Семантика перехода **не меняется** (ADR-027 §F: `will_renew=false`, `status` сохраняется, токены не трогаются) — меняется только строковый литерал имени.

**`KNOWN_EVENT_TYPES` = все 18 фактических типов** (факт №5) — ни один реальный тип не должен попадать в ветку «неизвестный `event_type`». Классификация (три непересекающихся множества, объединение = 18):

**1. `HANDLED_SUBSCRIPTION_EVENT_TYPES` (7)** — драйвят state-machine `subscriptions` ([§2.3](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица)):
`subscription_started`, `subscription_renewed`, `subscription_renewal_cancelled`, `subscription_expired`, `subscription_refunded`, `billing_issue_detected`, `access_level_updated`.

**2. `CONSUMABLE_EVENT_TYPES` (1)** — consumable token-паки ([§11.3](../modules/billing/03-architecture.md#113-consumable-token-паки-non_subscription_purchase-adr-038), ADR-038):
`non_subscription_purchase`.

**3. `CONSCIOUSLY_IGNORED_EVENT_TYPES` (10)** — известны провайдеру, но **осознанно** no-op на вебхуке (не «неизвестный тип»): персистятся в `billing_events` (`processed_at=NULL`), денежных/state-эффектов не производят, отвечают `200 {"status":"ignored","event_type":"<type>"}` + **INFO-диагностика** (`reason=unhandled_known_event`):
`subscription_renewal_reactivated`, `subscription_paused`, `subscription_deferred`, `trial_started`, `trial_converted`, `trial_renewal_cancelled`, `trial_renewal_reactivated`, `trial_expired`, `entered_grace_period`, `non_subscription_purchase_refunded`.

**Обоснование «осознанно игнорируем» (не денежный дефект):** для **подписочных** событий (реактивации/пауза/deferred/trial/grace) источник истины прав — Adapty, а `subscriptions` — кэш, который **самокорректируется** периодическим `getProfile`-resync (§3.1) и lazy-resync на гейте (§3.2). Права по этим событиям отражаются через `access_level_updated` + resync, поэтому явная обработка каждого нюанса на вебхуке **не требуется для корректности прав** — только сокращает latency (покрытую lazy-resync на горячем пути). Инвент новых непроверенных переходов отвергнут как over-engineering (см. Alternatives). **Исключение — `non_subscription_purchase_refunded`:** consumable-clawback resync'ом НЕ реконсилится (§11.3 не читает историю покупок), но списание consumable-кредитов — **продуктовое решение [Q-BILLING-6](../99-open-questions.md#q-billing-6)** (открыт); до его принятия событие осознанно no-op с диагностическим следом (не тихо). Промоушен любого из этих типов в явную обработку — при появлении продуктового требования ([Q-BILLING-8](../99-open-questions.md#q-billing-8)).

**Неизвестный тип** (Adapty ввёл новый `event_type` вне 18 — будущее расширение каталога) → `200 {"status":"ignored","event_type":"<type>"}` + **WARN-диагностика** (`reason=unknown_event`) как сигнал обновить `KNOWN_EVENT_TYPES`.

### D. Наблюдаемость отброшенных событий (требование, согласовано с 05-security)

Любое событие, **не** приведшее к штатной обработке (`ignored`/synthetic-key/`missing_customer_user_id`), обязано оставлять диагностический след. Состав полей — по [политике логирования 05-security](../05-security.md#observability-как-security-сигнал) (**значения payload логировать нельзя** — там платёжные/персональные данные):

- **Разрешено в диагностике:** `reason` (код), `event_type` (категория), корреляционные идентификаторы `customer_user_id` (= наш `user_id`, уже логируемый для корреляции) и Adapty `profile_id` (псевдонимный идентификатор профиля, не платёжное содержимое — аналогично логируемому `key_id`), результирующий `adapty_event_id`/маркер синтетического ключа.
- **ЗАПРЕЩЕНО в диагностике:** значения `event_properties` (суммы, цены, `transaction_id`, receipt), `email`, `idfa`/`idfv`/`advertising_id` (устройство/реклама), любой сырой payload.
- **Уровни:** `profile_event_id_absent` (fallback тира 2/3) → **WARN**; `unknown_event` (тип вне 18) → **WARN**; `unhandled_known_event` (осознанно игнорируемый) → **INFO**; `missing_customer_user_id` → **WARN** (уже есть alert, §2.4).
- **Метрика/alert (нормативное имя зарегистрировано [ADR-041 §E](ADR-041-adapty-webhook-field-extraction-real-payload.md)):** счётчик отброшенных/диагностируемых событий — **`lovable_billing_webhook_dropped_total`** (Counter, labels `reason`, `event_type`), зарегистрирован в единственном нормативном доме метрик [observability §2.7](../modules/observability/03-architecture.md#27-billing--quota-billing) — низкокардинальные лейблы (`reason`/`event_type` — категории, не идентификаторы). `reason="unknown_token_product"` покрывает и прежний consumable-alert `billing_unknown_token_product` (§11.3) — отдельной метрики для него нет. Grafana-alert при ненулевой скорости `profile_event_id_absent`/`unknown_event`. **Обязателен в текущем changeset — диагностический WARN/INFO-лог-след (выше); инструментация метрики — fast-follow по определённому имени §2.7, не деплой-гейт** ([ADR-041 §E](ADR-041-adapty-webhook-field-extraction-real-payload.md); при неработающем прод-resync — [ADR-041 §D](ADR-041-adapty-webhook-field-extraction-real-payload.md)/[Q-BILLING-10](../99-open-questions.md#q-billing-10) — метрика/alert критичны для сигнала ops).

### E. Обратная совместимость с историческими `billing_events.adapty_event_id` (без миграции)

`billing_events.adapty_event_id text UNIQUE NOT NULL` уже содержит исторические синтетические значения (`sim-*`, `e2e-*`, `manual-*`). Смена **источника** ключа (теперь `profile_event_id`/synthetic вместо `event_id||id`):

- **Тип/констрейнт колонки не меняется** — и старые значения, и новые (UUID `profile_event_id`, префикс `adapty-syn:`) суть `text`; UNIQUE не затрагивается. **Миграция/backfill НЕ требуются.**
- **Коллизий с историей нет:** UUID `profile_event_id` не начинается с `sim-`/`e2e-`/`manual-`; синтетические ключи несут отличимый префикс `adapty-syn:`. Исторические строки остаются валидны и идемпотентны (переотправка исторического sim-события по-прежнему матчится).
- **Отсутствие «испорченных» реальных строк:** баг приводил к тому, что реальные события **не персистились вовсе** (`missing_event_id` → нет строки), а не записывались с неверным ключом. Значит, реконсилить существующие данные не нужно — нет строк с «неправильным» ключом.
- **Восстановление 3 уже-дропнутых прод-событий (операционное, backend/ops — не architect):** Adapty вернул им 200 и **не переотправит**. Реконсиляция: подписочный **state** самокорректируется `getProfile`-resync (§3) автоматически; **consumable-начисления** (`non_subscription_purchase`) resync'ом НЕ восстанавливаются ⇒ требуют ручной переотправки/реобработки этих событий **или** компенсации через admin `/credits` ([admin §3](../modules/admin/02-api-contracts.md)). Зафиксировать как отдельную операционную задачу backend/ops при выкатке фикса.

### F. Testing-требование: контракты внешних провайдеров — на реальном/официальном payload

Дефект скрыли **самоподтверждающие** тесты (синтетический payload по нашей же ошибочной схеме `event_id`). Нормативно ([06-testing-strategy](../06-testing-strategy.md)): contract-тесты интеграции с внешним провайдером (Adapty webhook, `getProfile`) ОБЯЗАНЫ использовать **фактическую форму payload провайдера** (официальный пример: `profile_event_id` внутри `event_properties`, реальные имена `event_type`, реальный набор верхнеуровневых полей — факты №1/№2/№5), а **не** payload, сконструированный по нашим внутренним допущениям. Фикстура вебхука ведётся от официального образца Adapty со ссылкой на источник и дату сверки.

## Consequences

**Плюсы:** денежный путь больше не теряет события тихо — ключ дедупа выводится всегда, событие персистится в `billing_events` даже при отсутствии `profile_event_id`; контракт вебхука сверен с first-party документацией (устранён источник дефекта — вывод «по памяти»); `KNOWN_EVENT_TYPES` согласован с фактическим перечнем провайдера (нет ложного «unknown» на реальных типах), закрыт Q-BILLING-5; наблюдаемость: любой отброс оставляет диагностический след в рамках security-политики логирования; без миграции, без новой зависимости (`hashlib`/stdlib), без новых env.

**Минусы / осознанные риски:**
- **Синтетический fallback (тир 2/3)** — суррогат канонического UUID: тир 3 (хэш тела) даёт двойную обработку, если Adapty переотправит семантически-то-же событие с **байт-различным** телом (переупорядочивание ключей JSON). Триггерится только когда `profile_event_id` И transaction-id **оба** отсутствуют — что противоречит документированным свойствам purchase-событий; для денежных `non_subscription_purchase` риск близок к нулю. WARN-диагностика `profile_event_id_absent` делает срабатывание видимым.
- **Осознанно игнорируемые типы (§C.3)** — их эффект на права отражается с latency resync'а (для подписочных — покрыто lazy-resync на гейте; для `non_subscription_purchase_refunded` clawback отсутствует до Q-BILLING-6). Это документированное следствие, не дефект.
- Always-200 по-прежнему скрывает ввод от ретраев Adapty — диагностика только по нашим логам/`billing_events`/метрикам (неизменно относительно ADR-027).

## Alternatives

- **Оставить `event_id || id` (статус-кво).** Отвергнута: на реальном payload всегда `None` → тихий дроп денежного события (суть инцидента).
- **`profile_event_id` без fallback: отсутствует → `200 ignored` (как раньше `missing_event_id`).** Отвергнута: `event_properties` варьируются (факт №3) → сохраняется вектор тихого дропа на событиях без `profile_event_id`. Гарантия «никогда не дропнуть тихо» требует выводимого-всегда ключа.
- **Дедуп только по `transaction_id`.** Отвергнута: один `transaction_id` фигурирует в нескольких `event_type` (started + access_level_updated) → кросс-типовая коллизия, ложный `duplicate`, потеря события. Транзакция годна лишь как fallback, скоупленный `event_type`.
- **Явная обработка всех 18 типов немедленно** (переходы для trial/pause/deferred/reactivation/grace). Отвергнута как over-engineering: инвент непроверенных state-переходов на денежном пути рискованнее, чем опора на документированный source-of-truth (Adapty + resync); права реконсилятся resync'ом. Промоушен — при продуктовом требовании ([Q-BILLING-8](../99-open-questions.md#q-billing-8)).
- **Backfill/миграция `billing_events`.** Отвергнута: тип/констрейнт колонки не меняются, испорченных строк нет (баг не персистил события) — реконсилить нечего.
