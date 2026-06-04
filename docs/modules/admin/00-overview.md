# admin — Overview ([ADR-021](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

## Scope
Операторская админ-плоскость поверх user-facing API:
- **Аутентификация админ-эндпоинтов** одним секретом `ADMIN_API_KEY` (заголовок `X-Admin-Key`, dependency `require_admin`) — не RBAC-роли в БД.
- **Login-as** — `POST /v1/admin/login-as`: выпуск пользовательского Bearer `lv_<key_id>_<secret>` за указанного `user_id` без Apple Sign-In (создаёт юзера без `apple_sub`, если нет). Назначение: dev/тест-логин + операторская выдача токена.
- **Бонус-генерации (кредиты)** — начисление/коррекция (`POST /v1/admin/users/{user_id}/credits`) и просмотр баланса+квоты (`GET /v1/admin/users/{user_id}`). Кредиты — сверх плановой месячной квоты, накопительные (не обнуляются помесячно).

Работает в **dev И prod** — безопасность через секрет, **не** через среду.

## Out-of-scope
- RBAC-роли / per-operator-аудит (один общий `ADMIN_API_KEY`; per-operator — отдельный ADR при необходимости).
- Публичная Swagger-подача — админ-эндпоинты `include_in_schema=False` (не для iOS-клиента).
- Изменение пользовательского Bearer-флоу (`/auth/apple`) и квота-гейта генераций сверх интеграции кредитов.
- UI/админ-панель (только REST-контракт; фронта нет).

## Связи
- `auth` — `token_service` (login-as), upsert юзера без `apple_sub` ([auth §7](../auth/03-architecture.md#7-admin-login-as-upsert-юзера-без-apple_sub-adr-021)).
- `billing` — quota-gate/`billing/me` учитывают `users.bonus_generations_balance` ([billing §10](../billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021)).
- `api` — публичная OpenAPI скрывает `/v1/admin/*` ([api §B.5](../api/02-api-contracts.md#b5-скрытие-служебных--internal-эндпоинтов-из-публичной-схемы)).
