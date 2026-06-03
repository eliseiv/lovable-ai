# ADR — реестр архитектурных решений

| ADR | Заголовок | Статус | Дата |
|---|---|---|---|
| [ADR-001](ADR-001-state-machine-dispatcher.md) | State-machine + диспетчер (task-на-состояние) vs Celery chain | Accepted | 2026-06-02 |
| [ADR-002](ADR-002-nginx-mount-vs-baked.md) | Generic nginx + mount vs per-site baked image | Accepted | 2026-06-02 |
| [ADR-003](ADR-003-celery-vs-rq.md) | Celery vs RQ для фоновых джоб | Accepted | 2026-06-02 |
| [ADR-004](ADR-004-adapty-source-of-truth.md) | Adapty как источник истины (вебхуки + getProfile) | Accepted | 2026-06-02 |
| [ADR-005](ADR-005-no-progress-failure-signature.md) | No-progress detection через хэш сигнатуры фейла | Accepted | 2026-06-02 |
| [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md) | Celery-retry (инфра) vs доменный FIXING (build-fail) | Accepted | 2026-06-02 |
| [ADR-007](ADR-007-sign-in-with-apple.md) | Sign in with Apple → обмен на свой opaque Bearer | Accepted | 2026-06-02 |
| [ADR-008](ADR-008-indexed-api-key-lookup.md) | Индексируемый lookup API-key (`key_id`-префикс) vs O(N) argon2 | Accepted | 2026-06-02 |
| [ADR-009](ADR-009-billing-idempotency-resync-grace.md) | Billing: идемпотентность вебхуков + getProfile-ресинк (dual-source) + grace-teardown через beat | Accepted | 2026-06-02 |
| [ADR-010](ADR-010-build-sandbox-rootless-egress.md) | Изоляция build-песочницы: rootless Docker + egress-allowlist (закрытие TD-001) | Accepted | 2026-06-02 |
| [ADR-011](ADR-011-project-delete-gc.md) | `DELETE /projects/{id}` + полный GC ресурсов проекта (закрытие TD-003) | Accepted | 2026-06-02 |
| [ADR-012](ADR-012-sse-realtime-transport.md) | SSE realtime-транспорт статуса (reconnect/Last-Event-ID, replay из job_events, heartbeat) + polling fallback | Accepted | 2026-06-02 |
| [ADR-013](ADR-013-apns-push-from-job-events.md) | APNs push-доставка статуса из job_events (background, device_tokens, HTTP/2 + JWT ES256) | Accepted | 2026-06-02 |
| [ADR-014](ADR-014-edit-limit-revision-rollback.md) | Отдельный лимит правок (`monthly_edits`/`edit_usage_counters`) + rollback ревизий (re-deploy good-ревизии) | Accepted | 2026-06-02 |
| [ADR-015](ADR-015-observability-stack.md) | Observability stack: Prometheus (метрики) + Grafana (дашборды/алерты) + Sentry (ошибки, scrubbing секретов) | Accepted | 2026-06-02 |
| [ADR-016](ADR-016-scale-topology-redis-pool.md) | Multi-host scale-топология (ручной scale, разнесение очередей) + Redis connection pool (TD-007) + batch resync (TD-009) | Accepted | 2026-06-02 |
| [ADR-017](ADR-017-path-based-site-routing.md) | Path-based routing сайтов (`/s/{site_id}` + StripPrefix + Vite base-path) vs субдомены; закрывает Q-DEPLOY-2 (wildcard не нужен) | Accepted | 2026-06-03 |
| [ADR-018](ADR-018-prod-deployment-shared-traefik-cicd.md) | Prod-deployment: встраивание в общий edge-Traefik (`corelysite.shop`, сеть `web`, без своего SSL) + CI/CD (GitHub Actions → SSH deploy) | Accepted | 2026-06-03 |
| [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md) | Reconciler покрывает ВСЕ активные нетерминальные состояния + graceful-fail агента при недоступности LLM (`agent_unavailable`/`stuck_timeout`, прод-фикс concurrency-leak) | Accepted | 2026-06-03 |

> **Прод-фикс ADR-017 (2026-06-03):** path-режимное Traefik-правило обязано быть `Host(APPS_DOMAIN) && PathPrefix(/s/{site_id})` + явный `priority` (`SITE_ROUTER_PRIORITY`) — без `Host(...)` на общей сети `web` правило матчит чужие запросы. Зафиксировано в [ADR-017 §Fix](ADR-017-path-based-site-routing.md#fix-2026-06-03--host-обязателен-в-path-правиле-прод-инцидент) (не отдельный ADR — уточнение существующего решения).

Конвенция: `ADR-NNN-<slug>.md`, разделы Context / Decision / Consequences / Alternatives. Не противоречить действующему ADR без нового ADR.
