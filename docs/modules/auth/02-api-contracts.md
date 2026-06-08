# auth — API Contracts (Sprint 3)

Base: `https://api.domain/v1` · Ошибки: RFC-7807 (`application/problem+json`).

## Сводка endpoints

| Method | Path | Назначение | Auth | Success |
|---|---|---|---|---|
| POST | `/auth/apple` | Sign in with Apple → выдать наш Bearer-ключ | **Apple identity token** (в теле) | `200` |
| POST | `/auth/register` | регистрация по `user_id`+секрет (сервер генерирует оба) → Bearer ([ADR-024](../../adr/ADR-024-user-id-secret-authentication.md)) | **публичный** (rate-limit по IP) | `201` |
| POST | `/auth/login` | вход по `user_id`+секрет → новый Bearer ([ADR-024](../../adr/ADR-024-user-id-secret-authentication.md)) | **публичный** (rate-limit по IP + per-`user_id` лок) | `200` |
| POST | `/auth/secret` | set/rotate секрета текущего пользователя ([ADR-024 §5](../../adr/ADR-024-user-id-secret-authentication.md)) | Bearer | `200` |
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

## POST /auth/register ([ADR-024](../../adr/ADR-024-user-id-secret-authentication.md))
Регистрация **нового** аккаунта без Apple/админ-ключа. Сервер **сам** генерирует и `user_id`, и секрет; клиентский `user_id` **не принимается** (захват/коллизия аккаунта).

- **Auth:** НЕ Bearer (это и есть регистрация). Публичный; rate-limit по IP (тот же механизм, что `/auth/apple` — `check_login_rate_limit`, `rl:apple:{ip}`-семейство).
- **Body:**
```json
{ "device_label": "string?" }
```
- **Поведение:**
  - `user_id = new_user_id()` (`u_...`), `secret = new_token_secret()` (256 бит энтропии) — **оба на сервере**;
  - создать `users` (`apple_sub=NULL`, `adapty_customer_user_id=users.id`, `status='active'`) — как admin-created юзер ([ADR-021 §B](../../adr/ADR-021-admin-plane-and-bonus-credits.md));
  - записать `users.auth_secret_hash = argon2id(secret)` (секрет не хранится/не восстановим);
  - выдать Bearer через `token_service.issue_token()` (новая строка `api_tokens`, `device_label`).
- **`201`** (секрет показывается **ОДИН раз**) →
```json
{ "user_id": "u_...",
  "secret": "<256-bit-secret>",
  "api_key": "lv_<key_id>_<secret>",
  "token_id": "t_..." }
```
- **Ошибки:** `429` (превышен IP rate-limit — RFC-7807 + `Retry-After`), `422` (невалидное тело). Идемпотентность не требуется — каждый вызов создаёт новый аккаунт (защита от abuse — IP-лимит + quota-gate биллинга).

> `secret` И `api_key` возвращаются **единственный раз** — клиент обязан сохранить **оба**: `secret` нужен для будущего `/auth/login` с новых устройств, `api_key` — для немедленных запросов. Сервер хранит только `argon2id(secret)` и `key_id`+`argon2id(token-secret)`.

## POST /auth/login ([ADR-024](../../adr/ADR-024-user-id-secret-authentication.md))
Вход по сохранённым `user_id`+секрет (новое устройство / восстановление). Выдаёт **новый** Bearer (мульти-устройство — существующие токены не трогаются).

- **Auth:** НЕ Bearer (это и есть вход). Публичный; rate-limit по IP (`check_login_rate_limit`) **+ per-`user_id` лок** против перебора секрета одного аккаунта ([05-security §Клиентская аутентификация](../../05-security.md#клиентская-аутентификация-по-user_id--секрет-adr-024)).
- **Body:**
```json
{ "user_id": "u_...", "secret": "<secret>", "device_label": "string?" }
```
- **Проверка:** `SELECT auth_secret_hash FROM users WHERE id = :user_id` → **constant-time** `argon2.verify(auth_secret_hash, secret)`.
- **ЛЮБОЙ провал** (нет юзера / `auth_secret_hash IS NULL` / неверный секрет) → **единый `401`** без раскрытия причины (как `/auth/apple`, RFC-7807). Не раскрываем, существует ли `user_id`.
- **Успех:** выдать Bearer через `token_service.issue_token()` (новая строка `api_tokens`); сбросить per-`user_id` счётчик неудач.
- **`200`** →
```json
{ "api_key": "lv_<key_id>_<secret>",
  "token_id": "t_...",
  "user_id": "u_..." }
```
- **Ошибки:** `401` (любой провал аутентификации — единый, без раскрытия), `429` (IP rate-limit **или** per-`user_id` лок исчерпан — RFC-7807 + `Retry-After`), `422` (нет `user_id`/`secret`).

## POST /auth/secret ([ADR-024 §5](../../adr/ADR-024-user-id-secret-authentication.md))
Set/rotate секрета **текущего** пользователя (под Bearer). Закрывает «перенос/восстановление»: Apple-юзер (или register-юзер) ставит/меняет секрет на **своём** аккаунте для кросс-платформенного входа через `/auth/login`.

- **Auth:** Bearer (действует за уже вошедшего юзера — через Apple ИЛИ register/login).
- **Body:** пустое (`{}`) — сервер генерирует новый секрет.
- **Поведение:** `secret = new_token_secret()`; записать `users.auth_secret_hash = argon2id(secret)` (set, если был `NULL`; rotate, если был задан — старый секрет инвалидируется). Существующие `api_tokens` (Bearer-устройства) **НЕ отзываются** (ротация секрета ≠ отзыв сессий).
- **`200`** (секрет показывается **ОДИН раз**) →
```json
{ "user_id": "u_...", "secret": "<new-256-bit-secret>" }
```
- **Ошибки:** `401` (нет/невалидный Bearer), `429` (rate-limit 60/min на ключ — §03-architecture).

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
- Auth-провалы (`/auth/apple`, `/auth/login` и Bearer) → `401` без раскрытия конкретной непрошедшей проверки. На `/auth/login` единый `401` покрывает все случаи: нет юзера / `auth_secret_hash IS NULL` / неверный секрет ([ADR-024](../../adr/ADR-024-user-id-secret-authentication.md)).
- Превышение rate-limit (60/min на ключ; на публичных `/auth/*` — IP-лимит, на `/auth/login` дополнительно per-`user_id` лок) → `429` (RFC-7807, заголовок `Retry-After`). Контракт rate-limit — [03-architecture.md](03-architecture.md).
- **Валидационный `422`** (`POST /auth/apple` без `identity_token`) — тоже **`application/problem+json`** (RFC-7807, `status=422`), **не** дефолтный FastAPI `{detail:[...]}`. Покрывается **глобальным** app-level `RequestValidationError`-обработчиком ([modules/api/03-architecture.md → Обработчики ошибок](../api/03-architecture.md#обработчики-ошибок--rfc-7807-нормативно-все-ошибки-включая-422)) — единая точка для всех эндпоинтов, включая публичный-без-Bearer `/auth/apple` (прод-фикс 2026-06-04).
