# ADR-015 — Observability stack: Prometheus + Grafana + Sentry

**Статус:** Accepted · **Дата:** 2026-06-02 · **Спринт:** 6 (Observability, cost, scale)

## Context

К Sprint 6 пайплайн (4 LLM-агента, fix-loop, cost-ledger), realtime (SSE), push (APNs), build-ферма и billing реализованы и покрыты тестами, но **слепы в проде**: нет метрик стоимости ($/job, cache hit-rate), глубины fix-loop, queue depth, SSE/APNs-исходов, quota-rejection-rate, gc-lag. Накоплен остаточный долг наблюдаемости от финальных reviewer'ов S4/S5: [TD-010](../100-known-tech-debt.md#td-010) (gc-lag без метрики), [TD-011](../100-known-tech-debt.md#td-011) (SSE heartbeat-catchup), [TD-012](../100-known-tech-debt.md#td-012) (concurrency-block без метрики). Cost-калибровка ([TD-005](../100-known-tech-debt.md#td-005)/[TD-006](../100-known-tech-debt.md#td-006), [Q-COST-1](../99-open-questions.md#q-cost-1)) требует реальных данных $/job. Продуктовое решение [08 §6-1](../08-product-decisions.md#sprint-6--observability-cost-scale): **observability = Prometheus + Grafana + Sentry**. Комплаенс особых требований не предъявляет ([08 §6-5](../08-product-decisions.md#sprint-6--observability-cost-scale)). В docs уже есть отсылки на «Prometheus-канарейки» ([05-security](../05-security.md#observability-как-security-сигнал), [07-deployment Health/readiness](../07-deployment.md#health--readiness)) — этот ADR разворачивает их в исполняемый контракт.

## Decision

**Метрики — Prometheus** (`prometheus-client`), **дашборды/алерты — Grafana** (as code), **ошибки/трейсинг — Sentry** (`sentry-sdk`). Нормативный контракт — [modules/observability/03-architecture.md](../modules/observability/03-architecture.md).

1. **Экспозиция `/metrics`** — на FastAPI-app (ASGI internal-route, не под `/v1`, не публичный) **и** на Celery-воркерах/beat (`start_http_server(METRICS_PORT)`). Health/readiness `/healthz`/`/readyz` остаются отдельно (не заменяются метриками).

2. **Нормативная таблица метрик** (имя/тип/labels) — единый источник в [observability §2](../modules/observability/03-architecture.md#2-нормативная-таблица-метрик). Backend инструментирует символ-в-символ. Префикс `lovable_`.

3. **Запрет unbounded-labels.** `job_id`/`user_id`/`subdomain`/`apns_token` **не** идут в Prometheus-labels (взрыв кардинальности) — только ограниченные перечисления (state/agent/queue/reason/kind/result). Высококардинальные идентификаторы — в Sentry-теги и структурные логи. Per-user/per-job $-панели — из Postgres-datasource в Grafana, не из Prometheus.

4. **Sentry для FastAPI + Celery** с correlation `job_id`/`project_id`/`user_id` (Sentry-теги — единственное место, где они в observability) и **обязательным scrubbing секретов** (`before_send`-hook, denylist + regex `lv_`/`Bearer`/PEM). Список scrubатся = список секретов [05-security → Секреты](../05-security.md#секреты) (single normative source). `send_default_pii=False`. Пустой `SENTRY_DSN` → init no-op (как APNs без credentials).

5. **Grafana as code** — datasource + dashboards в `infra/grafana/` (provisioning), scrape-конфиг `infra/prometheus/prometheus.yml`. Все три — конфиг-артефакты (правило — [07-deployment.md](../07-deployment.md#правило-конфиг-артефакта-prometheusgrafana-sprint-6)): провизия devops, секреты из env.

6. **Алерты — Grafana alert rules** поверх метрик (gc-lag, budget-burn, build-queue-depth, resync-lag). On-call-интеграция (Alertmanager/PagerDuty), distributed tracing (OTel), лог-агрегация (Loki) — **out-of-scope S6** ([observability §00](../modules/observability/00-overview.md)).

## Consequences

- **(+)** Cost-калибровка ([TD-005](../100-known-tech-debt.md#td-005)/[TD-006](../100-known-tech-debt.md#td-006)) получает данные ($/job, cache hit-rate, tiering); [TD-010](../100-known-tech-debt.md#td-010)/[TD-011](../100-known-tech-debt.md#td-011)/[TD-012](../100-known-tech-debt.md#td-012) закрываются наблюдаемостью.
- **(+)** Sentry-scrubbing — формальная гарантия, что Adapty/APNs/Apple/S3-ключи и Bearer-секрет не утекают в трейсы (security-инвариант).
- **(+)** Pull-модель Prometheus + as-code Grafana вписываются в существующий compose/multi-host (scrape per-реплика/per-воркер); ручной scale (S6) не требует динамической service-discovery (статические targets + reload).
- **(−)** Новые долгоживущие сервисы (`prometheus`, `grafana`) в проде + новые зависимости (`prometheus-client`, `sentry-sdk`) — операционная нагрузка. Принято: без наблюдаемости prod-релиз рискован (slепой cost/runaway).
- **(−)** Дисциплина label-кардинальности обязательна — нарушение (`job_id` в label) деградирует Prometheus. Закреплено запретом в [observability §1](../modules/observability/03-architecture.md#1-экспозиция-metrics) + проверка reviewer.
- **(−)** Multiprocess-режим app требует явного выбора (`PROMETHEUS_MULTIPROC_DIR` vs один процесс на реплику) — иначе неполный registry. Зафиксировано в [observability §1](../modules/observability/03-architecture.md#1-экспозиция-metrics).

## Alternatives

- **OpenTelemetry + collector (метрики+трейсы+логи единым SDK).** Отвергнут для S6 как избыточный: целевой масштаб (несколько хостов, ручной scale) покрывается Prometheus-метриками + Sentry-ошибками + correlation-id в логах; OTel-spans — потенциальный поздний ADR, не нужен сейчас. Меньше движущихся частей.
- **Только Sentry (performance + метрики через Sentry).** Отвергнут: Sentry-метрики дороже и слабее для time-series дашбордов cost/queue; Prometheus+Grafana — индустриальный стандарт pull-метрик, дешёвый self-host.
- **Push-метрики (StatsD/Pushgateway).** Отвергнут: pull-модель Prometheus проще для stateless-реплик за Traefik (scrape per-target) и не требует промежуточного агрегатора; Pushgateway оправдан для short-lived-задач, но Celery-воркеры долгоживущие (экспонируют свой порт).
- **Per-user `user_id`-label в Prometheus для $/user.** Отвергнут (кардинальность) — $/user считается из Postgres-datasource в Grafana ([observability §2.2](../modules/observability/03-architecture.md#22-cost--llm-cost-ledger-llm_usage)).
