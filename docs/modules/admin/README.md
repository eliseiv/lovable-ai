# admin — операторская админ-плоскость ([ADR-021](../../adr/ADR-021-admin-plane-and-bonus-credits.md))

**Статус:** контракт зафиксирован (ADR-021), реализация — backend.

Операторская плоскость поверх user-facing API: аутентификация одним секретом `ADMIN_API_KEY` (не RBAC-роли), login-as по `user_id`, начисление/просмотр бонус-генераций (кредитов). Работает в **dev И prod** (безопасность — через секрет, не через среду). Все эндпоинты **скрыты** из публичной OpenAPI (`include_in_schema=False`).

## Документы
| Документ | Назначение |
|---|---|
| [02-api-contracts.md](02-api-contracts.md) | REST-контракт админ-эндпоинтов (`/v1/admin/*`) |
| [03-architecture.md](03-architecture.md) | `require_admin`, `ADMIN_API_KEY`-гейтинг, login-as, кредиты |

## Состав
- **`require_admin`** — FastAPI-dependency: `X-Admin-Key` constant-time против `ADMIN_API_KEY`; пусто/невалидно → `401`.
- **`POST /v1/admin/login-as`** — выпуск пользовательского Bearer за `user_id` (создаёт юзера без `apple_sub`, если нет).
- **`POST /v1/admin/users/{user_id}/credits`** — начислить/скорректировать бонус-генерации.
- **`GET /v1/admin/users/{user_id}`** — баланс кредитов + квота юзера.

## Зависимости
- `auth.token_service` — выпуск `lv_<key_id>_<secret>` (login-as).
- `billing` — quota-gate учитывает `users.bonus_generations_balance`; `GET /billing/me` отражает кредиты ([billing §10](../billing/03-architecture.md#10-бонус-генерации-кредиты-adr-021)).
- Data-model: `users.bonus_generations_balance`, `credit_grants` ([03-data-model](../../03-data-model.md#credit_grants-бонус-генерации-adr-021)).
- Env: `ADMIN_API_KEY` ([07-deployment → env-контракт](../../07-deployment.md#канонический-список-ключей)).

## Безопасность
- `ADMIN_API_KEY` — секрет уровня root-доступа (login-as = вход за любого юзера). Encrypted-at-rest, только secret-manager/GitHub Secrets, **не** в git/`docs`. Threat-model — [05-security → Админ-плоскость](../../05-security.md#админ-плоскость-adr-021).
- Пустой `ADMIN_API_KEY` → админ-плоскость отключена (`require_admin` всегда `401`).
