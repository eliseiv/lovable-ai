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

## Changelog
- 2026-06-02: создан bootstrap (architect).
- 2026-06-02: Sprint 3 — auth выделен в модуль [auth](../auth/README.md) (Sign in with Apple, индексируемый lookup, мульти-устройство); `api` маршрутизирует `/auth/*` и подключает Bearer-dependency. Добавлены endpoints `/auth/apple`, `/auth/tokens` в сводку.
- 2026-06-02: Sprint 5 — развёрнут realtime/edits-контракт: полный SSE `GET /jobs/{jid}/events` (reconnect/`Last-Event-ID`/replay из `job_events`/heartbeat/`done`/cross-tenant, [ADR-012](../../adr/ADR-012-sse-realtime-transport.md)); `POST /projects/{pid}/edits` с отдельным лимитом правок + `POST .../revisions/{n}/rollback` ([ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)); `POST/DELETE /v1/devices` (APNs регистрация, [ADR-013](../../adr/ADR-013-apns-push-from-job-events.md), модуль [notify](../notify/README.md)) (architect).
