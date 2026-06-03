# auth — API Contracts (Sprint 3)

Base: `https://api.domain/v1` · Ошибки: RFC-7807 (`application/problem+json`).

## Сводка endpoints

| Method | Path | Назначение | Auth | Success |
|---|---|---|---|---|
| POST | `/auth/apple` | Sign in with Apple → выдать наш Bearer-ключ | **Apple identity token** (в теле) | `200` |
| GET | `/auth/tokens` | список активных токенов (устройств) пользователя | Bearer | `200` |
| DELETE | `/auth/tokens/{id}` | отозвать токен (одно устройство / logout) | Bearer | `204` |

> Bearer-аутентификация **всех остальных** endpoint'ов (`/projects`, `/jobs`, `/billing`) — общая dependency, контракт описан в [03-architecture.md](03-architecture.md). Формат ключа `lv_<key_id>_<secret>` ([ADR-008](../../adr/ADR-008-indexed-api-key-lookup.md)).

## POST /auth/apple
Вход — Apple identity token (полученный iOS через Sign in with Apple). Верифицируется server-side ([ADR-007](../../adr/ADR-007-sign-in-with-apple.md)), upsert user по `apple_sub`, выдаётся наш Bearer-ключ.

- **Auth:** НЕ Bearer (это и есть логин). Аутентификация — валидность Apple identity token.
- **Body:**
```json
{ "identity_token": "<apple-jwt>",
  "nonce": "string?",
  "device_label": "string?" }
```
- **Верификация** (любой провал → `401`):
  - подпись по JWKS Apple (`kid` → ключ, кэш);
  - `iss == https://appleid.apple.com`;
  - `aud == APPLE_AUDIENCE` (bundle/Services ID);
  - `exp` не истёк, `iat`/`nbf` валидны;
  - `nonce` (если передан) совпадает с `nonce` в токене.
- **Upsert:** `apple_sub = token.sub` → найден user → его взять; не найден → создать `users` (+ `adapty_customer_user_id = users.id`).
- **Выдача токена:** сгенерировать `key_id` + `secret`, записать строку `api_tokens` (`key_hash = argon2id(secret)`, `device_label`), вернуть собранный ключ **один раз**.
- **`200`** →
```json
{ "api_key": "lv_<key_id>_<secret>",
  "token_id": "t_...",
  "user_id": "u_..." }
```
- **Ошибки:** `401` (невалидный/просроченный Apple-токен, неверный `aud`/`iss`/`nonce` — RFC-7807, не раскрываем какую проверку не прошёл), `422` (отсутствует `identity_token`).

> `api_key` возвращается **единственный раз** — клиент обязан сохранить. Сервер хранит только `key_id` + argon2-хэш `secret`.

## GET /auth/tokens
Список активных токенов (устройств) текущего пользователя.
- **Auth:** Bearer.
- **`200`** →
```json
{ "tokens": [
  { "id": "t_...", "key_id": "<key_id>", "device_label": "iPhone 15",
    "created_at": "...", "last_used_at": "...", "current": true } ] }
```
- `current: true` — токен, которым сделан текущий запрос. `key_id` показываем (он не секрет); `secret`/хэш — никогда.
- Возвращаются только `revoked_at IS NULL`.

## DELETE /auth/tokens/{id}
Отзыв токена (выход с одного устройства). Мягкий revoke (`revoked_at = now()`), строка сохраняется для аудита.
- **Auth:** Bearer.
- Авторизация владения: `{id}` обязан принадлежать `current_user`, иначе `404` (cross-tenant — не раскрываем существование).
- Идемпотентно: повтор по уже отозванному → `204`.
- **`204`** (no content). Ошибки: `401`, `404`.
- После revoke последующие запросы с этим ключом → `401` (lookup отфильтрует `revoked_at IS NOT NULL`).

## Конвенции ошибок (RFC-7807)
```json
{ "type": "https://api.domain/errors/unauthorized",
  "title": "Unauthorized",
  "status": 401,
  "detail": "Invalid or expired credentials." }
```
- Auth-провалы (`/auth/apple` и Bearer) → `401` без раскрытия конкретной непрошедшей проверки.
- Превышение rate-limit (60/min на ключ) → `429` (RFC-7807, заголовок `Retry-After`). Контракт rate-limit — [03-architecture.md](03-architecture.md).
- **Валидационный `422`** (`POST /auth/apple` без `identity_token`) — тоже **`application/problem+json`** (RFC-7807, `status=422`), **не** дефолтный FastAPI `{detail:[...]}`. Покрывается **глобальным** app-level `RequestValidationError`-обработчиком ([modules/api/03-architecture.md → Обработчики ошибок](../api/03-architecture.md#обработчики-ошибок--rfc-7807-нормативно-все-ошибки-включая-422)) — единая точка для всех эндпоинтов, включая публичный-без-Bearer `/auth/apple` (прод-фикс 2026-06-04).
