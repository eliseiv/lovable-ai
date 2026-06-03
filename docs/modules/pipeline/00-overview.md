# pipeline — Overview

## Scope
- State-machine генерации (`app/pipeline/state_machine`) + диспетчер по `generation_jobs.state`.
- 4 LLM-агента (`app/pipeline/agents/agent1..4`) через Anthropic SDK; промты в `app/pipeline/prompts/`.
- Human-in-the-loop пауза на `AWAITING_CLARIFICATION` (ноль компьюта) и событийный резюм.
- Самовосстанавливающийся цикл `FIXING→BUILDING→DEPLOYING→LIVE|FIXING` с 4 гардами (max attempts / budget / wall-clock / no-progress-by-signature); после валидного патча Agent 4 джоба идёт в `BUILDING` (пересборка нового source.tgz → dist), не сразу в `DEPLOYING`. Agent 4 читает `failure_log` из S3.
- Разграничение Celery-retry (транзиентный инфра-сбой) vs доменный FIXING (build-fail) — [ADR-006](../../adr/ADR-006-celery-retry-vs-domain-fixing.md).
- Cost-control: cost-ledger `llm_usage` (агрегат `spend_usd` в Postgres — источник истины бюджета), prompt caching, tiering моделей. Быстрый Redis-счётчик бюджета — опциональная оптимизация латентности гейта, Sprint 6 ([TD-006](../../100-known-tech-debt.md#td-006)), не входит в S2.
- Запись `job_events` (audit + SSE pub/sub в Redis).
- Celery beat: sweeper (экспайр зависших `AWAITING_CLARIFICATION` по TTL) + reconciler (crash-resume застрявших `BUILDING/DEPLOYING/FIXING`).

## Out-of-scope
- HTTP-эндпоинты — модуль `api`.
- Реальная сборка/деплой сайта (`vite build`, nginx, Traefik, health) — модуль `deploy`.
- Логика биллинга/квот — модуль `billing` (гейт на входе — в `api`).

## Зависимости
- Anthropic SDK (Claude), Postgres, Redis (очередь/pub-sub/счётчики), S3 (исходники/спека-ref).
- Модуль `deploy` (постановка `queue=build`).
