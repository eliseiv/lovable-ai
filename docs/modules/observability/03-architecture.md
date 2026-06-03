# observability — Architecture (исполняемый контракт Sprint 6)

> Фиксирует **полный исполняемый контракт** наблюдаемости: экспозиция `/metrics`, нормативная таблица метрик (имя/тип/labels), Grafana-дашборды + provisioning, Sentry (correlation + scrubbing), наблюдаемость scale-долга. Решения вынесены в [ADR-015](../../adr/ADR-015-observability-stack.md) (стек Prometheus/Grafana/Sentry) и [ADR-016](../../adr/ADR-016-scale-topology-redis-pool.md) (multi-host scale + Redis pool). Стек — [02-tech-stack.md](../../02-tech-stack.md); env — [07-deployment.md → env-контракт](../../07-deployment.md#канонический-список-ключей).

## 1. Экспозиция `/metrics`

Метрики собирает `prometheus-client` (Python). **Двойная экспозиция** — два процесса разной природы:

| Процесс | Как экспонируется | Scrape-target |
|---|---|---|
| **FastAPI app** (`app.api.main:app`, N stateless-реплик) | ASGI-эндпоинт `GET /metrics` (registry процесса), за Traefik как internal-route (не публичный `/v1`) | `api:<port>/metrics` per-реплика |
| **Celery worker** (`-Q llm`, `-Q build`) + **beat** | `prometheus_client.start_http_server(METRICS_PORT)` в worker-процессе (отдельный HTTP-порт, не через FastAPI — у воркера нет ASGI) | `worker:<METRICS_PORT>/metrics` per-воркер |

**Нормативные правила экспозиции:**
- `/metrics` на app — **internal**, не под `/v1`, не требует Bearer, доступен только из cluster/compose-сети (Prometheus-scrape), наружу через Traefik **не** публикуется. Cross-ref [05-security → сетевые границы](../../05-security.md#сетевые-границы).
- **Multiprocess-режим app:** uvicorn с несколькими worker-процессами требует `prometheus_client` multiprocess (env `PROMETHEUS_MULTIPROC_DIR`) — иначе каждый uvicorn-worker отдаёт свой неполный registry. Если app запускается одним процессом на реплику (рекомендация для stateless-реплик за Traefik — масштаб репликами контейнера, не процессами внутри), multiproc не требуется; выбор фиксируется devops в compose ([07-deployment.md](../../07-deployment.md)). Нормативно: **один из двух режимов обязан быть выбран явно**, «случайный» неполный per-process registry — баг.
- **`/healthz`/`/readyz`** ([07-deployment.md → Health/readiness](../../07-deployment.md#health--readiness)) — **остаются как есть**, не заменяются `/metrics` (liveness/readiness ≠ метрики). Уже реализованы (S1).
- Label-кардинальность: **запрещены** unbounded-labels (`job_id`, `user_id`, `subdomain`, `apns_token` как label — взрыв кардинальности). Высококардинальные идентификаторы идут в Sentry/логи (§4), **не** в Prometheus-labels. Допустимые labels — только ограниченные перечисления (state, agent, queue, reason, kind, result) из таблицы §2.

## 2. Нормативная таблица метрик

Единый источник истины по именам/типам/labels. Backend инструментирует **символ-в-символ**. Префикс всех app-метрик — `lovable_`. Гистограммы — с явными bucket'ами (указаны где критично). Counter-имена — с суффиксом `_total` по конвенции Prometheus.

### 2.1 Pipeline / jobs

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_jobs_total` | Counter | `kind` (`generation`/`edit`/`rollback`), `terminal_state` (`LIVE`/`FAILED`) | завершённые джобы по исходу | [pipeline](../pipeline/03-architecture.md) |
| `lovable_jobs_in_state` | Gauge | `state` (9 значений enum), `kind` | мгновенное число джоб в каждом state (для «зависших» в FIXING/BUILDING) | [pipeline state machine](../pipeline/03-architecture.md#state-machine) |
| `lovable_job_failed_total` | Counter | `reason` (перечень reason-кодов §C pipeline), `kind` | терминальные `FAILED` по `failure_reason` | [pipeline §C reason-коды](../pipeline/03-architecture.md#машинные-reason-коды-failure_reason-полный-перечень-sprint-2) |
| `lovable_build_duration_seconds` | Histogram | `result` (`success`/`fail`) | длительность `npm ci && vite build` в песочнице | [deploy](../deploy/03-architecture.md) |
| `lovable_fix_loop_depth` | Histogram | `terminal_state` | глубина fix-loop = итоговый `retry_count` джобы (canary runaway) | [pipeline §C(a)](../pipeline/03-architecture.md#a-hard-cap-max_fix_attempts--единственный-нормативный-источник-правила-инкремента-retry_count) |
| `lovable_no_progress_trips_total` | Counter | — | срабатывания гарда no-progress (калибровка нормализаторов [TD-005](../../100-known-tech-debt.md#td-005)) | [ADR-005](../../adr/ADR-005-no-progress-failure-signature.md) |

### 2.2 Cost / LLM (cost-ledger `llm_usage`)

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_job_cost_usd` | Histogram | `kind`, `terminal_state` | себестоимость джобы ($/job) на финализации (агрегат `generation_jobs.spend_usd`); buckets ориентир `[0.25,0.5,1,2,3,5,7.5,10]` | [pipeline cost-ledger](../pipeline/03-architecture.md#агенты-anthropic-sdk), [TD-006](../../100-known-tech-debt.md#td-006) |
| `lovable_llm_call_cost_usd_total` | Counter | `agent` (`agent1..agent4`), `model` (значение env `AGENTn_MODEL`) | суммарный $ по агенту/модели (подтверждение tiering, [08 §6-2](../../08-product-decisions.md#sprint-6--observability-cost-scale)) | [pipeline](../pipeline/03-architecture.md) |
| `lovable_llm_tokens_total` | Counter | `agent`, `model`, `token_type` (`input`/`output`/`cache_read`/`cache_write`) | токены по типу (cache-эффективность) | [02-tech-stack → prompt caching](../../02-tech-stack.md) |
| `lovable_llm_cache_hit_ratio` | Gauge | `agent` | доля cache_read-токенов от input (prompt-caching hit-rate) | skill `claude-api` |
| `lovable_llm_call_latency_seconds` | Histogram | `agent`, `model` | latency одного Claude-вызова | — |
| `lovable_user_spend_usd` | Gauge | — (агрегат, **без** per-user label) | суммарный месячный Claude-spend всех юзеров; per-user потолок `$50` энфорсится в коде, не в Prometheus-label | [billing §8](../billing/03-architecture.md#8-две-независимые-величины) |

> **$/user dashboard:** дашборд $/user строится из БД-панели (Grafana Postgres-datasource → `SELECT user_id, SUM(...) FROM llm_usage`), **не** из Prometheus per-user label (кардинальность). Cross-ref §3 (cost-дашборд).

### 2.3 SSE / realtime (api)

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_sse_streams_open` | Gauge | — | открытые SSE-стримы (глобально по процессу; сумма по репликам в Grafana) | [ADR-012](../../adr/ADR-012-sse-realtime-transport.md) |
| `lovable_sse_stream_duration_seconds` | Histogram | `close_reason` (`done`/`client_disconnect`/`heartbeat_timeout`) | длительность стрима до закрытия | [api SSE](../api/02-api-contracts.md) |
| `lovable_sse_rejected_total` | Counter | `reason` (`max_streams_per_key`) | отказы `429` по `SSE_MAX_STREAMS_PER_KEY` (per-ключ лимит) | [07-deployment SSE_MAX_STREAMS_PER_KEY](../../07-deployment.md#канонический-список-ключей) |
| `lovable_sse_heartbeat_catchup_total` | Counter | `result` (`tail_replayed`/`noop`) | срабатывания heartbeat-catchup до-чтения `job_events` ([TD-011](../../100-known-tech-debt.md#td-011) — после фикса метрика подтверждает работу страховки от потерянного pub/sub) | [TD-011](../../100-known-tech-debt.md#td-011) |

### 2.4 APNs (notify)

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_apns_push_total` | Counter | `result` (`delivered`/`invalidated`/`retry`/`drop`/`noop_no_credentials`), `apns_status` (`200`/`410`/`400`/`429`/`5xx`) | исход APNs-отправки по reason-коду Apple | [ADR-013](../../adr/ADR-013-apns-push-from-job-events.md), [notify](../notify/README.md) |
| `lovable_apns_tokens_invalidated_total` | Counter | `reason` (`unregistered_410`/`bad_token_400`) | инвалидации device-токенов | [notify DoD](../notify/README.md) |
| `lovable_apns_request_latency_seconds` | Histogram | — | latency HTTP/2-запроса к APNs | — |

### 2.5 Edit / rollback / deploy (deploy)

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_edit_outcome_total` | Counter | `outcome` (`live`/`edit_failed_rolled_back`) | исход edit-джобы (доля авто-rollback) | [ADR-014 §C](../../adr/ADR-014-edit-limit-revision-rollback.md) |
| `lovable_rollback_total` | Counter | `trigger` (`manual`/`auto_edit_fail`), `result` (`success`/`infra_error`) | rollback'и по триггеру | [deploy §7](../deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014) |
| `lovable_redeploy_duration_seconds` | Histogram | `kind` (`rollback`/`edit`/`generation`) | длительность re-deploy (health-200 без downtime) | [deploy §7](../deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014) |
| `lovable_dist_artifact_source_total` | Counter | `source` (`cache_hit`/`rebuild`) | re-deploy из S3-`dist`-артефакта (cache-hit) vs пересборка (rollback переиспользует `dist`, edit пересобирает) | [deploy §7](../deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014) |
| `lovable_project_gc_pending` | Gauge | — | soft-deleted проекты с незавершённым `project.gc` (наблюдаемость eventual-окна, [TD-010](../../100-known-tech-debt.md#td-010)) | [ADR-011](../../adr/ADR-011-project-delete-gc.md) |
| `lovable_project_gc_duration_seconds` | Histogram | `result` (`success`/`retry`) | длительность от `202` (soft-delete) до завершения GC ([TD-010](../../100-known-tech-debt.md#td-010)) | [TD-010](../../100-known-tech-debt.md#td-010) |

### 2.6 Queue / worker (scale)

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_queue_depth` | Gauge | `queue` (`llm`/`build`) | глубина очереди Celery (отставание воркеров) — exporter читает Redis-broker length | [01-architecture очереди](../../01-architecture.md#границы-инварианты) |
| `lovable_worker_busy` | Gauge | `queue` | занятые worker-слоты (utilization = busy/concurrency) | [ADR-016](../../adr/ADR-016-scale-topology-redis-pool.md) |
| `lovable_redis_pool_in_use` | Gauge | `pool` (`rate_limit`/`sse`/`budget`/`broker`) | занятые соединения переиспользуемого `ConnectionPool` ([TD-007](../../100-known-tech-debt.md#td-007) — подтверждает отсутствие per-request connect) | [TD-007](../../100-known-tech-debt.md#td-007) |
| `lovable_billing_resync_batch` | Histogram | `result` (`full`/`partial`) | размер обработанного батча `billing.resync` (батч+курсор [TD-009](../../100-known-tech-debt.md#td-009)) | [TD-009](../../100-known-tech-debt.md#td-009) |

### 2.7 Billing / quota (billing)

| Метрика | Тип | Labels | Что измеряет | Cross-ref |
|---|---|---|---|---|
| `lovable_quota_rejected_total` | Counter | `reason` (`no_entitlement`/`quota_exhausted`/`project_limit`/`concurrency_limit`/`edit_quota_exhausted`) | отказы `402` quota-gate по reason | [billing §4](../billing/03-architecture.md#4-entitlements--quota-gate) |
| `lovable_concurrency_block_by_kind_total` | Counter | `blocked_kind` (`generation`/`edit`/`rollback`), `holder_kind` (`generation`/`edit`/`rollback`) | отказ старта из-за занятого слота `max_concurrent_jobs`, с разбивкой «какой kind заблокирован каким kind» ([TD-012](../../100-known-tech-debt.md#td-012) — закрытие наблюдаемостью) | [billing §4.3](../billing/03-architecture.md#4-entitlements--quota-gate), [TD-012](../../100-known-tech-debt.md#td-012) |
| `lovable_adapty_resync_lag_seconds` | Gauge | — | максимальный возраст `subscriptions.synced_at` среди протухших (отставание ресинка) | [billing §3.1](../billing/03-architecture.md#31-периодический-celery-beat-billingresync) |
| `lovable_rate_limit_rejected_total` | Counter | `scope` (`api_key`/`apple_login_ip`) | отказы `429` token-bucket rate-limit (60/min) | [auth §5](../auth/03-architecture.md) |

## 3. Grafana дашборды

Дашборды — **as code** (JSON в `infra/grafana/dashboards/`, provisioning через `infra/grafana/provisioning/`). Datasources: **Prometheus** (метрики) + **Postgres** (per-user/per-job cost-панели, где Prometheus-кардинальность недопустима). Перечень нормативный:

| Дашборд | Файл | Панели (метрики) | Назначение |
|---|---|---|---|
| **Jobs pipeline** | `jobs-pipeline.json` | `lovable_jobs_in_state` (heatmap по state), `lovable_jobs_total`/`lovable_job_failed_total` (rate по reason), `lovable_build_duration_seconds` (p50/p95), `lovable_fix_loop_depth`, `lovable_no_progress_trips_total` | здоровье пайплайна, застрявшие джобы, runaway fix-loop |
| **Cost / $** | `cost.json` | `lovable_job_cost_usd` (p50/p95/max $/job vs `JOB_BUDGET_USD`=5), `lovable_llm_call_cost_usd_total` (по `agent`/`model` — tiering), `lovable_llm_cache_hit_ratio`, `lovable_llm_tokens_total`; **Postgres-панель** $/user (SUM по `llm_usage` vs `USER_MONTHLY_BUDGET_USD`=50) | калибровка бюджетов ([TD-005](../../100-known-tech-debt.md#td-005)/[TD-006](../../100-known-tech-debt.md#td-006)), подтверждение model-tiering |
| **SSE / realtime** | `sse-realtime.json` | `lovable_sse_streams_open` (sum по репликам), `lovable_sse_rejected_total` (429-rate), `lovable_sse_stream_duration_seconds`, `lovable_sse_heartbeat_catchup_total` | нагрузка realtime, исчерпание `SSE_MAX_STREAMS_PER_KEY` |
| **APNs** | `apns.json` | `lovable_apns_push_total` (по `result`/`apns_status`), `lovable_apns_tokens_invalidated_total`, `lovable_apns_request_latency_seconds` | здоровье push (drop/retry/invalidate по Apple-кодам) |
| **Build-ферма** | `build-farm.json` | `lovable_queue_depth{queue="build"}`, `lovable_worker_busy{queue="build"}`, `lovable_build_duration_seconds`, `lovable_redeploy_duration_seconds`, `lovable_dist_artifact_source_total`, `lovable_project_gc_pending`/`_duration` | utilization build-хостов, gc-lag, cache-hit re-deploy |
| **Billing / quota** | `billing-quota.json` | `lovable_quota_rejected_total` (по reason), `lovable_concurrency_block_by_kind_total`, `lovable_adapty_resync_lag_seconds`, `lovable_billing_resync_batch`, `lovable_rate_limit_rejected_total` | 402/429-rate, concurrency-блокировки ([TD-012](../../100-known-tech-debt.md#td-012)), resync-lag ([TD-009](../../100-known-tech-debt.md#td-009)) |

**Provisioning (нормативно, конфиг-артефакт):**
- `infra/grafana/provisioning/datasources/*.yml` — Prometheus + Postgres datasources (URL/креды из env, не хардкод).
- `infra/grafana/provisioning/dashboards/*.yml` — provider, указывающий на `infra/grafana/dashboards/*.json` (dashboards as code, версионируются в git).
- `infra/prometheus/prometheus.yml` — scrape-конфиг (targets: api-реплики, worker-метрик-порт, beat; интервал из `PROMETHEUS_SCRAPE_INTERVAL_S`).
- Все три — **конфиг-артефакты** (правило — [07-deployment.md → Правило конфиг-артефакта Prometheus/Grafana](../../07-deployment.md#правило-конфиг-артефакта-prometheusgrafana-sprint-6)): провижит devops, не хардкодятся в коде, секреты (Grafana admin, Postgres-datasource creds) — из env/secret-manager.

**Алерты (Grafana alert rules поверх метрик, in-scope как правила, on-call-интеграция out-of-scope §00):**
- `project_gc_pending > 0` дольше порога → gc-lag ([TD-010](../../100-known-tech-debt.md#td-010)).
- `lovable_job_cost_usd` p95 приближается к `JOB_BUDGET_USD` → budget-burn ([TD-006](../../100-known-tech-debt.md#td-006)).
- `lovable_queue_depth{queue="build"}` устойчиво растёт → нужен ручной scale build-хостов ([ADR-016](../../adr/ADR-016-scale-topology-redis-pool.md)).
- `lovable_adapty_resync_lag_seconds` выше `2×BILLING_RESYNC_INTERVAL_S` → resync отстаёт ([TD-009](../../100-known-tech-debt.md#td-009)).

## 4. Sentry

Инструментация исключений для **обоих** рантаймов: FastAPI (`sentry_sdk` + ASGI/Starlette integration) и Celery (`CeleryIntegration`). DSN — env `SENTRY_DSN` (пусто → Sentry-init **no-op**, как APNs без credentials: фича неактивна, процесс цел — нормативно).

**Correlation (нормативно):** на каждый запрос/таску в Sentry-scope проставляются теги `job_id`, `project_id`, `user_id` (через `sentry_sdk.set_tag` в middleware api / в обёртке Celery-таски по `job_id` аргументу). Это **единственное** место, где высококардинальные идентификаторы попадают в observability (в Prometheus-labels они запрещены, §1).

**Scrubbing секретов (нормативно — `before_send`/`before_breadcrumb` hook):** из событий Sentry **обязаны** вырезаться (никогда не утекают):
- `ANTHROPIC_API_KEY`, `ADAPTY_API_KEY`, `ADAPTY_WEBHOOK_SECRET`, `SEED_API_KEY`, `S3_ACCESS_KEY`/`S3_SECRET_KEY`;
- APNs `.p8` содержимое (`APNS_AUTH_KEY`), provider-JWT, `apns_token` (маскируется — согласовано с [05-security → APNs](../../05-security.md#apns-push-sprint-5-adr-013): `.p8`/JWT никогда не логируются);
- Bearer-ключ пользователя `lv_<key_id>_<secret>` — в Sentry допустим **только** `key_id`, секретная часть вырезается (согласовано с [05-security → Аутентификация](../../05-security.md#аутентификация): «в логах только `key_id`, никогда `secret`»);
- Apple identity token, DNS-provider token, Postgres/Redis DSN-пароли.

Реализация scrubbing — `before_send`-hook с denylist ключей + regex на token-паттерны (`lv_`, `Bearer `, PEM-блоки). **Default-PII off** (`send_default_pii=False`). Single normative source списка секретов — [05-security → Секреты](../../05-security.md#секреты); §4 ссылается на него и добавляет правило «эти же значения scrubятся в Sentry».

**Sampling:** `traces_sample_rate` из `SENTRY_TRACES_SAMPLE_RATE` (дефолт низкий, напр. `0.05`, чтобы не жечь quota), `environment` = `settings.environment` (`dev`/`prod`).

## 5. Cost-control калибровка ([TD-005](../../100-known-tech-debt.md#td-005)/[TD-006](../../100-known-tech-debt.md#td-006))

### 5.1 Нормализаторы сигнатуры фейла ([TD-005](../../100-known-tech-debt.md#td-005))
- **Донастройка** списка нормализаторов ([ADR-005](../../adr/ADR-005-no-progress-failure-signature.md)) на реальных build/health-логах, накопленных за S1–S5. Метрика-драйвер — `lovable_no_progress_trips_total` (§2.1): аномально высокая/низкая частота относительно `lovable_job_failed_total{reason="no_progress"}` сигналит о ложных trip/false-negative.
- **Опциональное версионирование лога по витку:** при потребности в пост-мортемах — писать `logs/{job_id}/attempt-{retry_count}.log` вместо перезаписи `build.log` (нормативный источник правила перезаписи — [pipeline §F](../pipeline/03-architecture.md#f-failure_log-в-s3); версионирование добавляется **как опция**, не меняя того, что Agent 4 читает лог последней попытки). Решение «версионировать или нет» принимается по факту наличия пост-мортем-потребности; до этого перезапись остаётся нормативной.

### 5.2 Redis budget-счётчик ([TD-006](../../100-known-tech-debt.md#td-006)) — опциональная оптимизация латентности гейта

> **Postgres остаётся source-of-truth бюджета** ([pipeline §C(b)](../pipeline/03-architecture.md#b-cost-cap-budget_usd) — не пересматривается). Redis-счётчик — **read-through кэш-гейт** перед чтением Postgres, не замена.

Механизм (нормативный контракт оптимизации):
- **Ключ:** `budget:{job_id}` (Redis), TTL = `JOB_WALL_CLOCK_BUDGET_S` (живёт не дольше джобы).
- **Запись:** после каждой записи строки `llm_usage` (cost-ledger) воркер делает `INCRBYFLOAT budget:{job_id} <cost_usd>` — атомарный инкремент дельты стоимости вызова. Та же транзакция, что пишет `generation_jobs.spend_usd` в Postgres (Postgres — авторитет; Redis — производное ускорение).
- **Чтение на гейте (pre-LLM, [pipeline §C(b)](../pipeline/03-architecture.md#b-cost-cap-budget_usd)):** сначала `GET budget:{job_id}`; если `>= budget_usd` → `FAILED(budget_exhausted)` без обращения к Postgres. **Cache-miss/отсутствие ключа** (TTL истёк, Redis рестартнул, crash-resume) → **fallback: прочитать `generation_jobs.spend_usd` из Postgres** (source-of-truth) и **пере-засеять** `budget:{job_id}` из БД-значения (`SET`). Никогда не пропустить гейт из-за отсутствия Redis-ключа.
- **Сверка/инвариант:** Redis-значение — кэш, может разойтись при сбое; **авторитет всегда Postgres**. На финализации джобы сверка не требуется (TTL чистит ключ). Метрика `lovable_redis_pool_in_use{pool="budget"}` (§2.6) подтверждает использование пула, не per-request connect.
- **Закрытие как «не требуется»:** если профилирование (`lovable_llm_call_latency_seconds` + БД-latency гейта) показывает, что Postgres-гейт укладывается в бюджет латентности на целевом масштабе — [TD-006](../../100-known-tech-debt.md#td-006) закрывается как «оптимизация не нужна» без реализации Redis-счётчика. Решение фиксируется по данным дашборда Cost.

### 5.3 Model tiering ([08 §6-2](../../08-product-decisions.md#sprint-6--observability-cost-scale))
- Маппинг агент→роль→модель — **единый нормативный источник:** [pipeline §Агенты → Tiering моделей](../pipeline/03-architecture.md#агенты-anthropic-sdk) (целевое: AGENT1 Interviewer=Sonnet, AGENT2 Spec=Opus, AGENT3 Builder=Opus, AGENT4 Fixer=Sonnet). Здесь — только **наблюдаемость подтверждения tiering**, без дублирования значений.
- Дашборд Cost подтверждает применённый tiering: `lovable_llm_call_cost_usd_total{agent,model}` показывает фактическую модель по каждому агенту; расхождение факта с нормативным маппингом — сигнал калибровки. Эффект приведения env-дефолтов к целевым значениям (задача S6-калибровки, см. [07-deployment env-контракт](../../07-deployment.md#контракт-переменных-окружения-environment-reference)) виден на `lovable_llm_call_cost_usd_total{agent="agent1"}` (Interviewer→Sonnet) и `{agent="agent4"}` (Fixer→Sonnet) до/после.

## 6. Scale-наблюдаемость и закрытие долга (cross-ref [ADR-016](../../adr/ADR-016-scale-topology-redis-pool.md))

Детальный scale-контракт (топология, ручной scale, Redis pool) — [07-deployment.md → Прод-топология](../../07-deployment.md#прод-топология) и [ADR-016](../../adr/ADR-016-scale-topology-redis-pool.md). Здесь — **наблюдаемость** закрытия долга:

| TD | Что реализуется в S6 | Метрика/проверка |
|---|---|---|
| [TD-007](../../100-known-tech-debt.md#td-007) | переиспользуемый Redis `ConnectionPool`/клиент-синглтон взамен per-request `from_url`/`aclose` в `rate_limit`/SSE/budget | `lovable_redis_pool_in_use{pool}` (§2.6) — стабильное число соединений под нагрузкой, не пилообразное |
| [TD-008](../../100-known-tech-debt.md#td-008) | batched `list_projects` live_url — один `WHERE project_id IN (...)` вместо N+1 | integration-тест: N проектов → 1 запрос (счётчик SQL) ([06-testing §S6](../../06-testing-strategy.md)) |
| [TD-009](../../100-known-tech-debt.md#td-009) | `billing.resync` `.limit(BATCH)` + курсор по `synced_at ASC` | `lovable_billing_resync_batch` + `lovable_adapty_resync_lag_seconds` (§2.6/2.7) |
| [TD-010](../../100-known-tech-debt.md#td-010) | gc-lag метрика/алерт | `lovable_project_gc_pending`/`_duration` (§2.5) + Grafana-alert (§3) |
| [TD-011](../../100-known-tech-debt.md#td-011) | SSE heartbeat-catchup до-чтение `job_events` по таймауту | `lovable_sse_heartbeat_catchup_total` (§2.3) |
| [TD-012](../../100-known-tech-debt.md#td-012) | метрика concurrency-block + продуктовое решение (оставить rollback/edit в cap) | `lovable_concurrency_block_by_kind_total` (§2.7) |

> **Продуктовое решение по [TD-012](../../100-known-tech-debt.md#td-012):** rollback/edit **остаются** в cap `max_concurrent_jobs` (текущее нормативное поведение [billing §4.3](../billing/03-architecture.md#4-entitlements--quota-gate) не меняется); долг закрывается **наблюдаемостью** (метрика `lovable_concurrency_block_by_kind_total`), а не изменением семантики слота. Это решение S6 (см. [TD-012](../../100-known-tech-debt.md#td-012) — план погашения). Если дашборд Billing покажет существенную долю блокировок generation со стороны rollback на Free — пересмотр выносится отдельным продуктовым решением/ADR, не в S6.
