# observability — Overview (Sprint 6)

## Цель
Сделать пайплайн, cost, realtime-доставку и build-ферму **наблюдаемыми** перед prod-релизом: метрики (Prometheus), дашборды (Grafana), трейсинг исключений (Sentry). Дать cost-калибровке ([TD-005](../../100-known-tech-debt.md#td-005)/[TD-006](../../100-known-tech-debt.md#td-006)) реальные данные ($/job, cache hit-rate, fix-loop depth) и закрыть наблюдаемостью остаточный долг S4/S5 ([TD-010](../../100-known-tech-debt.md#td-010)/[TD-011](../../100-known-tech-debt.md#td-011)/[TD-012](../../100-known-tech-debt.md#td-012)).

## In-scope (Sprint 6)
- Экспозиция `/metrics` (FastAPI-app + Celery llm/build воркеры + beat).
- Нормативная таблица метрик (jobs, build, fix-loop, cost/токены/cache, SSE, APNs, edit/rollback, queue depth, worker utilization, billing, gc-lag, concurrency-block).
- Grafana-дашборды (jobs pipeline, cost/$, SSE/realtime, APNs, build-ферма, billing) + provisioning as code.
- Sentry: инструментация исключений FastAPI + Celery, correlation `job_id`/`project_id`/`user_id`, scrubbing секретов.
- Cost-калибровка: донастройка нормализаторов сигнатуры фейла + опц. версионирование лога ([TD-005](../../100-known-tech-debt.md#td-005)); опциональный Redis budget-счётчик `INCRBYFLOAT` как read-through кэш-гейт ([TD-006](../../100-known-tech-debt.md#td-006)); подтверждение model-tiering ([08 §6-2](../../08-product-decisions.md#sprint-6--observability-cost-scale)).
- Scale-наблюдаемость: Redis `ConnectionPool` ([TD-007](../../100-known-tech-debt.md#td-007)), batched `list_projects` ([TD-008](../../100-known-tech-debt.md#td-008)), batch+курсор resync ([TD-009](../../100-known-tech-debt.md#td-009)).

## Out-of-scope (S6, явно)
- **Autoscaling (авто).** В S6 — только **ручной** scale (`docker compose scale`/replicas, [08 §6-4](../../08-product-decisions.md#sprint-6--observability-cost-scale)). Авто-масштабирование — позже.
- **Distributed tracing (OpenTelemetry spans).** Не вводим: метрики (Prometheus) + ошибки (Sentry) + correlation-id в логах достаточны для целевого масштаба. Если потребуется — отдельным ADR.
- **Алертинг как платформа (Alertmanager/PagerDuty).** Алерты определяются как Grafana-alert-правила поверх метрик (gc-lag, budget-burn, 429-rate). Внешняя on-call-интеграция — out-of-scope S6.
- **Комплаенс/аудит-экспорт.** Особых требований нет ([08 §6-5](../../08-product-decisions.md#sprint-6--observability-cost-scale)).
- **Логовая агрегация (Loki/ELK).** Структурные JSON-логи с `job_id`-correlation уже есть ([05-security → Observability](../../05-security.md#observability-как-security-сигнал)); централизованный лог-стор — out-of-scope S6.

## Зависимости
- **api** — `/metrics` на FastAPI-app, SSE-метрики, 402-rate quota-gate.
- **pipeline** — jobs-by-state, build duration, fix-loop depth, cost-ledger (`llm_usage` → $/job, токены, cache), queue depth.
- **billing** — quota 402-rate, resync-lag, concurrency-block-by-kind.
- **deploy** — gc-pending/duration, rollback/re-deploy, dist-cache-hit.
- **notify** — APNs delivered/invalidated/retry/drop.
- **devops** — Prometheus/Grafana образы в compose, scrape-конфиг, dashboard-provisioning, Sentry DSN-провизия, build-host scale.
