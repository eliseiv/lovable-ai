# Модуль `api`

**Статус:** spec (Sprint 0) · **Владелец кода:** `app/api`

REST-фасад для iOS. Stateless. Auth (Bearer), приём джоб (постановка в очередь), статус (poll/SSE), резюм пайплайна, правки, биллинг-endpoints.

## Граница
- НЕ делает LLM-вызовов и сборок — только Postgres + Redis (enqueue / read status).
- Читает статус из Postgres/Redis, пишет команды в очередь Celery.

## Документы
- [00-overview.md](00-overview.md) — scope / out-of-scope
- [03-architecture.md](03-architecture.md) — слои, middleware, конвенции
- [02-api-contracts.md](02-api-contracts.md) — REST-контракт iOS (полный)

## DoD
- Все endpoints из контракта реализованы со статусами/RFC-7807.
- `Idempotency-Key`, `402`-гейтинг, `401` без Bearer.
- OpenAPI соответствует контракту.
- **Публичная Swagger/OpenAPI-документация** соответствует нормативному стандарту ([02-api-contracts.md → Публичная API-документация](02-api-contracts.md#публичная-api-документация-swaggeropenapi--нормативный-стандарт)): рус. язык, denylist внутренних маркеров (нет «Sprint»/«ADR-»/«TD-»/имён агентов в `/openapi.json`), доменные `tags`, `include_in_schema=False` для internal-эндпоинтов, рус. глобальные метаданные.

## Changelog
- 2026-06-02: создан bootstrap (architect).
- 2026-06-02: Sprint 3 — auth выделен в модуль [auth](../auth/README.md) (Sign in with Apple, индексируемый lookup, мульти-устройство); `api` маршрутизирует `/auth/*` и подключает Bearer-dependency. Добавлены endpoints `/auth/apple`, `/auth/tokens` в сводку.
- 2026-06-02: Sprint 5 — развёрнут realtime/edits-контракт: полный SSE `GET /jobs/{jid}/events` (reconnect/`Last-Event-ID`/replay из `job_events`/heartbeat/`done`/cross-tenant, [ADR-012](../../adr/ADR-012-sse-realtime-transport.md)); `POST /projects/{pid}/edits` с отдельным лимитом правок + `POST .../revisions/{n}/rollback` ([ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)); `POST/DELETE /v1/devices` (APNs регистрация, [ADR-013](../../adr/ADR-013-apns-push-from-job-events.md), модуль [notify](../notify/README.md)) (architect).
- 2026-06-03: **прод — публичная Swagger-документация как справочник для iOS (тема B).** Зафиксирован нормативный стандарт публичной OpenAPI-схемы в [02-api-contracts.md](02-api-contracts.md#публичная-api-документация-swaggeropenapi--нормативный-стандарт): рус. язык, denylist внутренних маркеров (Sprint/ADR/TD/имена агентов Interviewer/Spec/Builder/Fixer/модулей/оркестрации), доменные `tags` (Аутентификация/Проекты/Джобы генерации/Правки и ревизии/Устройства/Биллинг), `include_in_schema=False` для `/metrics`/`/healthz`/`/readyz`, webhook Adapty с пометкой S2S, рус. глобальные `title`/`description`/`version`, grep-чек-лист для reviewer/qa. ТЗ для backend (architect).
