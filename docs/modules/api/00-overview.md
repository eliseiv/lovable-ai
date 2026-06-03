# api — Overview

## Scope
- Аутентификация iOS-клиента (Bearer API-key, argon2-хэш). **Sprint 3:** Sign in with Apple + индексируемый lookup + мульти-устройство — выделено в модуль [auth](../auth/README.md); `api` подключает Bearer-dependency и маршрутизирует `/auth/*`.
- Создание проектов и старт генерации (постановка задачи в очередь, `202`).
- Статус джобы: polling (`GET /jobs/{id}`, канонический) и SSE (`GET /jobs/{id}/events`).
- Уточняющие вопросы и приём ответов (резюм пайплайна).
- Post-delivery правки и история ревизий.
- Биллинг-endpoints (`/billing/me`, приём вебхуков Adapty) — детально в модуле `billing`, маршрутизация и middleware-гейтинг — здесь.
- Idempotency, RFC-7807 ошибки, quota-gate middleware.

## Out-of-scope
- LLM-вызовы и сборка — модуль `pipeline` / `deploy` (воркеры).
- Логика подписок и ресинк Adapty — модуль `billing`.
- Прямой доступ к Docker/Traefik — модуль `deploy`.

## Зависимости
- Postgres (чтение/запись метаданных), Redis (enqueue, статус, SSE pub/sub).
- Модуль `billing` для quota-gate.
