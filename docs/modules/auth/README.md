# Модуль `auth`

**Статус:** реализован (Sprint 3) · **Владелец кода:** `app/auth`

Аутентификация iOS-клиента: **Sign in with Apple** → обмен Apple identity token на наш opaque Bearer API-key; мульти-устройство (N токенов на user); индексируемый O(1) lookup ключа; отзыв токенов. Закрывает [TD-004](../../100-known-tech-debt.md#td-004).

## Граница
- НЕ хранит пароли (Sign in with Apple, [ADR-007](../../adr/ADR-007-sign-in-with-apple.md)).
- Выдаёт/верифицирует **наш** Bearer-ключ ([ADR-008](../../adr/ADR-008-indexed-api-key-lookup.md)); HTTP-роутинг и Bearer-dependency для остальных endpoint'ов — общая с модулем `api`.
- Маппинг user ↔ Adapty (`customer_user_id = user.id`) — создаётся здесь при первом входе; логика подписок — модуль `billing`.

## Документы
- [00-overview.md](00-overview.md) — scope / out-of-scope
- [02-api-contracts.md](02-api-contracts.md) — `POST /v1/auth/apple`, `GET/DELETE /v1/auth/tokens`
- [03-architecture.md](03-architecture.md) — Apple verify, токен-модель, lookup, rate-limit, concurrency cap, миграция S1→S3

## DoD (Sprint 3) — ✅ выполнен (реализован и покрыт тестами)
- ✅ `POST /v1/auth/apple`: верификация Apple identity token (JWKS, iss/aud/exp/nonce), upsert user по `apple_sub`, выдача Bearer-ключа `lv_<key_id>_<secret>`.
- ✅ Индексируемый O(1) lookup токена по `key_id` + один constant-time argon2-verify ([TD-004](../../100-known-tech-debt.md#td-004) **closed**).
- ✅ Мульти-устройство: N активных `api_tokens` на user.
- ✅ Отзыв: `DELETE /v1/auth/tokens/{id}` + список `GET /v1/auth/tokens`.
- ✅ Rate-limit 60 req/min на ключ (Redis token bucket, `app/auth/rate_limit.py`); cap конкурентных генераций (1 free / 3 pro; в S3 — дефолт free).
- ✅ Cross-tenant изоляция подтверждена тестом.
- ✅ Миграционный путь с S1 seeded-ключа без слома существующих тестов.

**Приёмочный пункт, ещё НЕ прогнанный:** живой E2E с реальным Apple token flow (боевой `APPLE_AUDIENCE`) + Claude+Docker — не прогонялся (нет окружения). Auth проверен через мок JWKS + эфемерный Postgres/Redis (329 passed, coverage 87.26%) — см. [docs/README.md → Статус Sprint 3](../../README.md#статус-sprint-3-реализовано).

**Tech-debt, выявленный при реализации S3 (minor):** [TD-007](../../100-known-tech-debt.md#td-007) (Redis без пула в `rate_limit.py` → Sprint 6), [TD-008](../../100-known-tech-debt.md#td-008) (N+1 в `list_projects`, pre-existing S1 → Sprint 5/6).

## Changelog
- 2026-06-02: создан модуль (architect, Sprint 3 контракт).
- 2026-06-02: статус → **реализован**; DoD выполнен (qa 329 passed / coverage 87.26%, reviewer `production_ready: true`); TD-004 closed; заведены TD-007/TD-008 (architect, финальная актуализация Sprint 3).
