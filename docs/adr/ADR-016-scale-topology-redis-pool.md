# ADR-016 — Multi-host scale-топология + Redis connection pool (ручной scale)

**Статус:** Accepted · **Дата:** 2026-06-02 · **Спринт:** 6 (Observability, cost, scale)

## Context

Архитектура с S1 предполагает stateless-API за Traefik и раздельные очереди `llm`/`build` ([01-architecture](../01-architecture.md)), а [07-deployment → Прод-топология](../07-deployment.md) фиксирует «API → N реплик, build-воркеры на отдельных хостах». Продуктовое решение [08 §6-3/6-4](../08-product-decisions.md#sprint-6--observability-cost-scale): **деплой-таргет = несколько хостов** (API stateless + отдельные build-хосты), **autoscaling — ручной в S6** (авто позже). Накопленный scale-долг требует закрытия перед prod: [TD-007](../100-known-tech-debt.md#td-007) (Redis-подключение без пула в hot-path rate-limit/SSE), [TD-009](../100-known-tech-debt.md#td-009) (resync без батча/курсора), [TD-008](../100-known-tech-debt.md#td-008) (N+1 в `list_projects`). Build-воркеры уже на rootless Docker + egress-allowlist ([ADR-010](../adr/ADR-010-build-sandbox-rootless-egress.md)) — этот ADR фиксирует **как они масштабируются** и **как app переиспользует Redis-соединения**.

## Decision

**Multi-host ручной scale** с разнесением очередей по хостам и переиспользуемым Redis `ConnectionPool`. Нормативный контракт топологии — [07-deployment.md → Прод-топология](../07-deployment.md#прод-топология); наблюдаемость — [observability §6](../modules/observability/03-architecture.md#6-scale-наблюдаемость-и-закрытие-долга-cross-ref-adr-016).

1. **Топология прода (несколько хостов):**
   - **API stateless** — N реплик контейнера `api` за Traefik/LB (масштаб репликами контейнера, не процессами внутри — упрощает Prometheus-registry, [observability §1](../modules/observability/03-architecture.md#1-экспозиция-metrics)). Состояние — только Postgres/Redis.
   - **LLM-воркеры** (`-Q llm`) — масштаб по **rate-limit Claude** (не по CPU); могут жить на app-хостах или отдельных.
   - **Build-воркеры** (`-Q build`) — на **отдельных build-хостах** с rootless Docker + egress-proxy ([ADR-010](../adr/ADR-010-build-sandbox-rootless-egress.md)); `queue=build` исполняет также `project.gc` ([ADR-011](../adr/ADR-011-project-delete-gc.md)).
   - **Beat** — единственный экземпляр (sweeper/reconciler/resync/subscription_sweep).
   - Postgres/Redis/S3 — managed или отдельные узлы.

2. **Разнесение очередей по хостам** — `llm` и `build` запускаются как раздельные Celery-worker-пулы (`-Q llm` / `-Q build`) на разных хостах с разным профилем ресурсов (rate-limit vs CPU+изоляция). Уже зафиксировано в [01-architecture](../01-architecture.md#границы-инварианты); ADR подтверждает для multi-host.

3. **Ручной scale (S6)** — масштабирование числом реплик/воркеров вручную: `docker compose up --scale api=N --scale worker-build=M` (compose) или эквивалент `replicas:` в оркестраторе. **Авто-scaling — out-of-scope S6** ([08 §6-4](../08-product-decisions.md#sprint-6--observability-cost-scale)); метрики `lovable_queue_depth`/`lovable_worker_busy` ([observability §2.6](../modules/observability/03-architecture.md#26-queue--worker-scale)) дают сигнал «пора добавить хост вручную» (Grafana-alert).

4. **Redis `ConnectionPool` (закрытие [TD-007](../100-known-tech-debt.md#td-007)):** единый переиспользуемый `ConnectionPool`/клиент-синглтон на процесс (web/worker) вместо `from_url(...)`+`aclose()` per-request в `app/auth/rate_limit.py`, SSE и (опц.) budget-счётчике. Параметры пула — env `REDIS_POOL_*` ([07-deployment.md](../07-deployment.md#канонический-список-ключей)). Наблюдаемость — `lovable_redis_pool_in_use` ([observability §2.6](../modules/observability/03-architecture.md#26-queue--worker-scale)).

5. **Resync батч+курсор (закрытие [TD-009](../100-known-tech-debt.md#td-009)):** `billing.resync` — `.limit(BATCH)` + курсор `synced_at ASC` (самые протухшие первыми, хвост на следующих тиках). Размер — env `BILLING_RESYNC_BATCH_SIZE`. Метрики — `lovable_billing_resync_batch`/`lovable_adapty_resync_lag_seconds`.

6. **N+1 `list_projects` (закрытие [TD-008](../100-known-tech-debt.md#td-008)):** batched live_url — один `WHERE project_id IN (...)` на список активных деплоев вместо запроса-на-проект.

## Consequences

- **(+)** [TD-007](../100-known-tech-debt.md#td-007)/[TD-008](../100-known-tech-debt.md#td-008)/[TD-009](../100-known-tech-debt.md#td-009) закрыты; hot-path Redis и list/resync масштабируются.
- **(+)** Ручной scale + метрики queue depth — минимальный достаточный механизм для целевого масштаба без сложности авто-scaling.
- **(+)** Разнесение очередей по хостам изолирует CPU-bound build-ферму (с песочницей) от latency-sensitive API/LLM.
- **(−)** Ручной scale требует оператора (наблюдение дашборда Build-ферма/queue depth + ручное добавление хоста). Принято для S6; авто — поздний этап.
- **(−)** `ConnectionPool` per-process — нужно следить за размером пула vs `max_clients` Redis при росте реплик (env `REDIS_POOL_MAX_CONNECTIONS`); метрика `lovable_redis_pool_in_use` страхует.

## Alternatives

- **Авто-scaling (HPA/KEDA по queue depth) в S6.** Отвергнут продуктово ([08 §6-4](../08-product-decisions.md#sprint-6--observability-cost-scale)) — ручной scale в S6, авто позже. Метрики уже готовят почву (queue depth → будущий KEDA-trigger).
- **Единый общий worker-пул (`llm`+`build` вместе).** Отвергнут (нарушает инвариант раздельных очередей [01-architecture](../01-architecture.md#границы-инварианты)) — CPU-bound сборка глушила бы latency-чувствительные LLM-таски, и build-изоляция требует отдельных хостов.
- **Оставить per-request Redis-connect ([TD-007](../100-known-tech-debt.md#td-007) as-is).** Отвергнут для prod-масштаба — TCP-connect+teardown на каждый аутентифицированный запрос даёт лишнюю latency/нагрузку на Redis под N репликами.
- **Stateful sticky-sessions для SSE.** Отвергнут — SSE durability держится на replay из `job_events` по `Last-Event-ID` ([ADR-012](../adr/ADR-012-sse-realtime-transport.md)), реплики остаются stateless; sticky не нужен.
