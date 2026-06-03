# Модуль `notify`

**Статус:** **реализован (Sprint 5)** · **Владелец кода:** `app/notify`

Push-нотификации статуса джобы на iOS через **APNs** ([ADR-013](../../adr/ADR-013-apns-push-from-job-events.md), [Q-CLIENT-1](../../99-open-questions.md#q-client-1) resolved). Доставляет значимые переходы (`LIVE`/`FAILED`/`AWAITING_CLARIFICATION`) когда приложение в фоне (SSE/polling — только foreground, [ADR-012](../../adr/ADR-012-sse-realtime-transport.md)).

## Граница
- **Не** источник истины статуса — best-effort нотификация. Источник истины — `job_events`/`GET /jobs/{jid}`.
- **Не** блокирует пайплайн: ставится как отдельная Celery-задача после коммита перехода; потеря push не ломает джобу.
- Регистрация устройств (`POST/DELETE /v1/devices`) маршрутизируется модулем `api`; отправка push — Celery-задача `notify.apns_push`.

## Документы
- [00-overview.md](00-overview.md) — scope / out-of-scope
- [03-architecture.md](03-architecture.md) — device-регистрация, триггер из job_events, APNs-клиент (HTTP/2 + JWT ES256), обработка ошибок токенов

## DoD
- `device_tokens` (регистрация/upsert/инвалидация), endpoints `POST /v1/devices` / `DELETE /v1/devices/{apns_token}`.
- Celery `notify.apns_push` триггерится на `LIVE`/`FAILED`/`AWAITING_CLARIFICATION` после коммита перехода.
- APNs HTTP/2 клиент (`httpx[http2]`) + provider-JWT ES256 (`PyJWT[crypto]`, кэш JWT по `APNS_JWT_TTL_S`).
- `410 Unregistered`/`400 BadDeviceToken` → инвалидация токена; `429`/`5xx` → Celery retry; нет credentials → no-op (фича неактивна, пайплайн цел).
- Cross-tenant: push только на устройства владельца джобы.

## Changelog
- 2026-06-02: создан spec Sprint 5 — APNs push из job_events ([ADR-013](../../adr/ADR-013-apns-push-from-job-events.md)), `device_tokens`, `/v1/devices` (architect).
- 2026-06-02: **реализован Sprint 5** — `notify.apns_push`, `device_tokens`, `POST/DELETE /v1/devices` реализованы и покрыты тестами (backend+devops → reviewers → qa 553 passed / coverage 84% → reviewer `production_ready: true`). Без APNs `.p8`/`APNS_*` (внешняя зависимость пользователя) push — no-op (проверено тестом). Остаточный live-приёмочный пункт: реальный push боевым `.p8` — в составе E2E Sprint 5 (не прогонялся). [Q-CLIENT-1](../../99-open-questions.md#q-client-1) resolved (architect-актуализация).
