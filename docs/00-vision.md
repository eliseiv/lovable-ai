# 00 — Vision: продукт, цели, NFR

## Продукт

Backend-сервис, обслуживающий iOS-приложение. Пользователь в приложении вводит промт («сделай лендинг для кофейни с меню и формой записи»), backend через пайплайн из 4 LLM-агентов на Claude генерирует статический сайт (Vite), реально собирает его, разворачивает в собственном nginx-контейнере за Traefik и возвращает пользователю живой URL `{subdomain}.apps.domain`.

Ключевая особенность — **human-in-the-loop**: первый агент задаёт уточняющие вопросы и пайплайн реально приостанавливается до ответа пользователя, не сжигая компьют. И **bounded self-healing**: при фейле сборки агент-фиксер чинит проект в ограниченном цикле с жёсткими гардами.

## Цели

1. От промта до реально доступного `live_url` — полностью автоматически.
2. Прод-grade надёжность: crash-resumable пайплайн, ретраи по шагам, idempotency.
3. Мульти-юзер с биллингом: квоты генераций по тарифу (Adapty), изоляция проектов.
4. Безопасное исполнение **недоверенного** LLM-сгенерированного кода (sandbox).
5. Контроль затрат на Claude: бюджеты per-job/per-user, prompt caching, tiering моделей.

## Пользователи и сценарии

- **iOS-клиент (конечный пользователь)** — создаёт проект промтом, отвечает на вопросы, получает live URL, заказывает post-delivery правки.
- **Adapty (server-to-server)** — присылает вебхуки событий подписки; источник истины по правам доступа.

## Нефункциональные требования (NFR)

| NFR | Формулировка | Где обеспечивается |
|---|---|---|
| **Async-first** | API не делает LLM-вызовов и сборок инлайн. Всё тяжёлое — в Celery. API отвечает `202` и кладёт работу в очередь. | [01-architecture.md](01-architecture.md), модуль `api` |
| **Human-in-the-loop** | На `AWAITING_CLARIFICATION` пайплайн реально приостановлен: ноль задач в очереди, ноль компьюта. Резюм событийный (`POST /answers`). | модуль `pipeline` |
| **Untrusted code execution** | LLM-код собирается только в throwaway-песочнице (rootless/gVisor), cap-drop ALL, non-root, egress-allowlist, ресурс-лимиты. Никогда на хосте воркера. | [05-security.md](05-security.md), модуль `deploy` |
| **Cost control** | Per-job/per-user бюджеты ($), cost-ledger `llm_usage` (агрегат `spend_usd` в Postgres — источник истины бюджета), prompt caching, tiering моделей Sonnet/Opus. Runaway обрывается гардами. Быстрый Redis-счётчик бюджета — оптимизация латентности гейта при масштабе (Sprint 6, [TD-006](100-known-tech-debt.md#td-006)). | модуль `pipeline`, [05-security.md](05-security.md) |
| **Multi-tenant** | Изоляция проектов по `user_id` на уровне БД-запросов и сети сайтов; cross-tenant — часть threat-model. | [05-security.md](05-security.md), модуль `api` |
| **Bounded self-healing** | Цикл `FIXING→BUILDING→DEPLOYING→LIVE|FIXING` (после фикса — пересборка) ограничен: hard cap `max_fix_attempts`, cost cap, wall-clock cap, no-progress detection. Исчерпание → `FAILED`. | модуль `pipeline` |
| **Crash-resumable** | Пайплайн — task-на-состояние + диспетчер по колонке `state`, не один длинный task. Падение воркера → ретрай шага. | модуль `pipeline`, [ADR-001](adr/ADR-001-state-machine-dispatcher.md) |
| **Observability** | Структурные JSON-логи с `job_id` correlation, `job_events` как бизнес-трейс, Prometheus (jobs by state, build duration, fix-loop depth, $/job), Sentry, health/readiness. | модуль `pipeline`, [07-deployment.md](07-deployment.md) |
| **Scalability** | API stateless → реплики. LLM-воркеры масштабируются по rate-limit Claude, build-воркеры по CPU на отдельных хостах. Раздельные очереди = независимое масштабирование. | [01-architecture.md](01-architecture.md) |
| **Idempotency** | `Idempotency-Key` на `POST /projects` и `/edits`; вебхуки Adapty идемпотентны по `adapty_event_id`; `/answers` идемпотентен на уже продвинувшейся джобе. | модуль `api`, модуль `billing` |
| **Simplicity** | Простейшее работающее: MinIO вместо AWS в dev, generic nginx + mount вместо per-site образа, монолитный репозиторий с раздельными воркерами вместо микросервисов. | [02-tech-stack.md](02-tech-stack.md), [ADR-002](adr/ADR-002-nginx-mount-vs-baked.md) |

## Вне scope (явно)

- Не редактор сайтов в реальном времени — только генерация + post-delivery правки через агента.
- Не поддержка серверного кода в генерируемых сайтах на старте — только статика (Vite build).
- Не собственный процессинг платежей — биллинг полностью делегирован Adapty (iOS ведёт покупку, backend гейтит).

## Метрики успеха

- E2E happy-path (промт → LIVE URL) проходит на dev-стеке автоматически.
- Fix-loop depth и $/job на дашбордах ограничены гардами (нет runaway).
- `402` корректно гейтит генерацию при отсутствии прав/исчерпании квоты.
