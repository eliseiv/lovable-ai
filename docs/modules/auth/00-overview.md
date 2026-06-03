# auth — Overview

## Scope
- **Sign in with Apple** (`POST /v1/auth/apple`): приём Apple identity token, server-side верификация (JWKS Apple, `iss`/`aud`/`exp`/`nonce`), upsert `users` по `apple_sub`, выдача нашего opaque Bearer-ключа ([ADR-007](../../adr/ADR-007-sign-in-with-apple.md)).
- **Токен-модель** (`api_tokens`): opaque-ключ `lv_<key_id>_<secret>`, argon2id-хэш секрета, индексируемый `key_id` для O(1) lookup ([ADR-008](../../adr/ADR-008-indexed-api-key-lookup.md)). Закрывает [TD-004](../../100-known-tech-debt.md#td-004).
- **Мульти-устройство:** N активных токенов на `user_id`.
- **Управление токенами:** список устройств (`GET /v1/auth/tokens`), отзыв (`DELETE /v1/auth/tokens/{id}` / logout).
- **Bearer-аутентификация запросов:** dependency `Authorization: Bearer <key>` → `current_user` через индексируемый lookup. Заменяет O(N) argon2-перебор S1.
- **Rate-limit** (60 req/min на ключ, Redis token bucket) и **cap конкурентных генераций** (1 free / 3 pro) — контракт энфорса; реальный tier подключается в S3.5.
- **Маппинг user ↔ Adapty:** `adapty_customer_user_id = user.id` создаётся при первом входе iOS.

## Out-of-scope
- Логика подписок/квот (бизнес-квота, `402`-гейтинг по тарифу) — модуль `billing` (Sprint 3.5). В S3 cap конкурентных джоб использует **дефолт free**-tier (заглушка `access_level`), реальный `access_level` подключит S3.5.
- HTTP-роутинг прочих endpoint'ов и middleware-сборка — модуль `api`.
- Ротация ключей (полноценная) — поздний спринт; в S3 только базовый revoke.
- Сторонние OAuth-провайдеры (Google и пр.) — не в S3 ([ADR-007](../../adr/ADR-007-sign-in-with-apple.md) → Alternatives).

## Зависимости
- Postgres (`users`, `api_tokens`), Redis (token bucket rate-limit, счётчик конкурентных джоб).
- JWKS Apple (`https://appleid.apple.com/auth/keys`) — верификация identity token (кэш ключей).
- Модуль `billing` (S3.5) — реальный `access_level` для cap конкурентных джоб.

## Внешние зависимости (отмечены)
- `APPLE_AUDIENCE` (bundle id / Services ID) — задаётся при наличии Apple Developer-конфигурации iOS-приложения. До этого конфигурируется через env; тесты — mock JWKS.
