# ADR-009 — Billing: идемпотентность вебхуков + getProfile-ресинк (dual-source) + grace-teardown через beat

**Статус:** Accepted · **Дата:** 2026-06-02 · **Sprint:** 3.5

Уточняет [ADR-004](ADR-004-adapty-source-of-truth.md) (Adapty как источник истины) до **исполняемой** модели Sprint 3.5. Закрывает исполняемые аспекты [Q-BILLING-1/2/3](../99-open-questions.md#q-billing-1) (resolved).

## Context

Adapty — источник истины по подпискам ([ADR-004](ADR-004-adapty-source-of-truth.md)): вебхуки + `getProfile`-ресинк, локальный `subscriptions` — кэш. Для реализации Sprint 3.5 нужно зафиксировать три механизма, оставленные на уровне «направление» в ADR-004:

1. **Двойной источник прав без двойной обработки.** Вебхук может прийти дважды (Adapty ретраит доставку) и может быть потерян. Нужна модель, где повтор безопасен, а потеря самокорректируется.
2. **Поведение `subscriptions` при expire/refund/billing_issue.** Продуктовое решение ([08 §3.5-6](../08-product-decisions.md#sprint-35--billing-adapty), [Q-BILLING-1](../99-open-questions.md#q-billing-1)): сайты при отмене не сносятся сразу — grace 7 дней, renew в grace отменяет снос. Нужна исполняемая state-machine и механизм отложенного teardown.
3. **Подключение реального `access_level`** на место S3-заглушки free в concurrency-cap ([auth §6](../modules/auth/03-architecture.md)).

## Decision

**Dual-source с идемпотентным ledger + grace-teardown через Celery-beat sweeper.**

### A. Идемпотентность вебхуков
- `billing_events.adapty_event_id` UNIQUE (`= webhook event_id`) — **единственная** точка дедупа. Повтор → `200` no-op.
- Insert ledger-строки + апдейт `subscriptions` — в одной транзакции с `processed_at=now()`. Ошибка апдейта → откат, `processed_at=NULL`, Adapty повторит/добьёт ресинк.
- Неизвестный `customer_user_id` → `billing_events(user_id=NULL, processed_at=NULL)` (не теряем событие).

### B. Dual-source прав (вебхук = primary, getProfile = reconcile)
- Вебхуки — основной канал апдейта `subscriptions` (низкая latency на гейте: читаем кэш).
- `getProfile`-ресинк — fallback на пропущенные вебхуки: периодический Celery-beat (`BILLING_RESYNC_INTERVAL_S`) + lazy по требованию на гейте при протухшем `synced_at`. Ресинк не перетирает более свежее вебхук-состояние (сравнение по таймстампу). При недоступности Adapty на гейте — **fail-open на кэш**.
- Rate-limit к Adapty Server-side API (Redis token-bucket) — ресинк не превышает квоту Adapty.

### C. Grace-teardown через beat
- `subscriptions.status ∈ {active, expired, grace, billing_issue}`; на гейте проходят `active`+`grace`. State-machine переходов по `event_type` — [modules/billing/03-architecture.md §2.3/§6](../modules/billing/03-architecture.md#23-маппинг-event_type--subscriptions-нормативная-таблица) (single normative source).
- expire/refund → `status=grace`, `grace_until = +GRACE_PERIOD_DAYS` (7). Доступ генерации в grace сохраняется; pending-teardown сайтов.
- Celery-beat `billing.subscription_sweep` (`SUBSCRIPTION_SWEEP_INTERVAL_S`): `status='grace' AND grace_until < now()` → teardown `active` сайтов пользователя (переиспользует deploy-teardown `docker rm -f` + route, идемпотентно) → `status=expired`.
- Renew/started в grace → `status=active`, `grace_until=NULL`. Гонка renew↔sweep разрешается `SELECT ... FOR UPDATE` строки `subscriptions` в sweep.

### D. Замена S3-заглушки
`entitlements.resolve_access_level`/`resolve_max_concurrent_jobs` отдают реальный `access_level` из `subscriptions`; модуль `auth` (concurrency-cap, [auth §6](../modules/auth/03-architecture.md)) и quota-gate берут лимиты из `plan_quotas` по реальному уровню вместо хардкода free.

## Consequences

**Плюсы:** повтор вебхука безопасен (UNIQUE); потеря вебхука самокорректируется ресинком; отложенный teardown реализует продуктовую grace-политику без спецлогики в горячем пути; renew-в-grace тривиально отменяет teardown (обнуление `grace_until`); fail-open на кэш не блокирует пользователя при недоступности Adapty.
**Минусы:** окно рассинхронизации вебхук↔ресинк (mitigation — интервал ресинка + lazy на гейте); grace-teardown — eventual (зависит от интервала sweep, допустимо ±`SUBSCRIPTION_SWEEP_INTERVAL_S`); требует beat-процесса (уже есть — sweeper/reconciler S2).

## Alternatives
- **Идемпотентность через app-level lock вместо UNIQUE.** Отвергнута: UNIQUE-constraint — атомарный дедуп на уровне БД, проще и надёжнее гонок.
- **Немедленный teardown при expire (без grace).** Отвергнута: противоречит продуктовому решению [08 §3.5-6](../08-product-decisions.md#sprint-35--billing-adapty) (renew в grace → сайт остаётся).
- **getProfile на каждом гейте (без кэша).** Отвергнута в [ADR-004](ADR-004-adapty-source-of-truth.md): latency + зависимость от Adapty на горячем пути.
- **Синхронный teardown в обработчике вебхука expire.** Отвергнута: вебхук должен отвечать быстро (`200` Adapty), teardown — фоновая операция; grace-окно в принципе требует отложенности.
