"""Нормативная таблица Prometheus-метрик lovable_* (Sprint 6, ADR-015).

Единый источник истины по именам/типам/labels — docs/modules/observability/03-architecture.md
§2. Backend инструментирует символ-в-символ. Префикс всех app-метрик — `lovable_`.
Counter-имена — с суффиксом `_total` (конвенция Prometheus). Гистограммы — с явными
bucket'ами где критично.

ЗАПРЕТ unbounded-labels (§1, ADR-015 §3): `job_id`/`user_id`/`subdomain`/`apns_token` НЕ
идут в labels (взрыв кардинальности) — только ограниченные перечисления (state/agent/queue/
reason/kind/result). Высококардинальные идентификаторы — в Sentry-теги и логи.

Все метрики регистрируются в дефолтном глобальном REGISTRY prometheus_client. Экспозиция:
  - FastAPI app — ASGI-эндпоинт /metrics (см. app/observability/exposition.py);
  - Celery worker/beat — start_http_server(METRICS_PORT) (см. app/observability/exposition.py).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- 2.1 Pipeline / jobs ---

jobs_total = Counter(
    "lovable_jobs_total",
    "Завершённые джобы по исходу (kind/terminal_state).",
    ["kind", "terminal_state"],
)
jobs_in_state = Gauge(
    "lovable_jobs_in_state",
    "Мгновенное число джоб в каждом state (зависшие в FIXING/BUILDING).",
    ["state", "kind"],
)
job_failed_total = Counter(
    "lovable_job_failed_total",
    "Терминальные FAILED по failure_reason.",
    ["reason", "kind"],
)
build_duration_seconds = Histogram(
    "lovable_build_duration_seconds",
    "Длительность npm ci && vite build в песочнице.",
    ["result"],
)
fix_loop_depth = Histogram(
    "lovable_fix_loop_depth",
    "Глубина fix-loop = итоговый retry_count джобы (canary runaway).",
    ["terminal_state"],
    buckets=(0, 1, 2, 3, 4, 5, 7, 10),
)
no_progress_trips_total = Counter(
    "lovable_no_progress_trips_total",
    "Срабатывания гарда no-progress (калибровка нормализаторов TD-005).",
)

# --- 2.2 Cost / LLM (cost-ledger llm_usage) ---

job_cost_usd = Histogram(
    "lovable_job_cost_usd",
    "Себестоимость джобы ($/job) на финализации (агрегат spend_usd).",
    ["kind", "terminal_state"],
    buckets=(0.25, 0.5, 1, 2, 3, 5, 7.5, 10),
)
llm_call_cost_usd_total = Counter(
    "lovable_llm_call_cost_usd_total",
    "Суммарный $ по агенту/модели (подтверждение tiering).",
    ["agent", "model"],
)
llm_tokens_total = Counter(
    "lovable_llm_tokens_total",
    "Токены по типу (cache-эффективность).",
    ["agent", "model", "token_type"],
)
llm_cache_hit_ratio = Gauge(
    "lovable_llm_cache_hit_ratio",
    "Доля cache_read-токенов от input (prompt-caching hit-rate).",
    ["agent"],
)
llm_call_latency_seconds = Histogram(
    "lovable_llm_call_latency_seconds",
    "Latency одного Claude-вызова.",
    ["agent", "model"],
)
user_spend_usd = Gauge(
    "lovable_user_spend_usd",
    "Суммарный месячный Claude-spend всех юзеров (без per-user label).",
)

# --- 2.3 SSE / realtime (api) ---

sse_streams_open = Gauge(
    "lovable_sse_streams_open",
    "Открытые SSE-стримы (глобально по процессу).",
)
sse_stream_duration_seconds = Histogram(
    "lovable_sse_stream_duration_seconds",
    "Длительность стрима до закрытия.",
    ["close_reason"],
)
sse_rejected_total = Counter(
    "lovable_sse_rejected_total",
    "Отказы 429 по SSE_MAX_STREAMS_PER_KEY.",
    ["reason"],
)
sse_heartbeat_catchup_total = Counter(
    "lovable_sse_heartbeat_catchup_total",
    "Срабатывания heartbeat-catchup до-чтения job_events (TD-011).",
    ["result"],
)

# --- 2.4 APNs (notify) ---

apns_push_total = Counter(
    "lovable_apns_push_total",
    "Исход APNs-отправки по reason-коду Apple.",
    ["result", "apns_status"],
)
apns_tokens_invalidated_total = Counter(
    "lovable_apns_tokens_invalidated_total",
    "Инвалидации device-токенов.",
    ["reason"],
)
apns_request_latency_seconds = Histogram(
    "lovable_apns_request_latency_seconds",
    "Latency HTTP/2-запроса к APNs.",
)

# --- 2.5 Edit / rollback / deploy (deploy) ---

edit_outcome_total = Counter(
    "lovable_edit_outcome_total",
    "Исход edit-джобы (доля авто-rollback).",
    ["outcome"],
)
rollback_total = Counter(
    "lovable_rollback_total",
    "Rollback'и по триггеру.",
    ["trigger", "result"],
)
redeploy_duration_seconds = Histogram(
    "lovable_redeploy_duration_seconds",
    "Длительность re-deploy (health-200 без downtime).",
    ["kind"],
)
dist_artifact_source_total = Counter(
    "lovable_dist_artifact_source_total",
    "Re-deploy из S3-dist (cache_hit) vs пересборка (rebuild).",
    ["source"],
)
project_gc_pending = Gauge(
    "lovable_project_gc_pending",
    "Soft-deleted проекты с незавершённым project.gc (eventual-окно, TD-010).",
)
project_gc_duration_seconds = Histogram(
    "lovable_project_gc_duration_seconds",
    "Длительность от 202 (soft-delete) до завершения GC (TD-010).",
    ["result"],
)

# --- 2.6 Queue / worker (scale) ---

queue_depth = Gauge(
    "lovable_queue_depth",
    "Глубина очереди Celery (отставание воркеров).",
    ["queue"],
)
worker_busy = Gauge(
    "lovable_worker_busy",
    "Занятые worker-слоты (utilization = busy/concurrency).",
    ["queue"],
)
redis_pool_in_use = Gauge(
    "lovable_redis_pool_in_use",
    "Занятые соединения переиспользуемого ConnectionPool (TD-007).",
    ["pool"],
)
billing_resync_batch = Histogram(
    "lovable_billing_resync_batch",
    "Размер обработанного батча billing.resync (TD-009).",
    ["result"],
)

# --- 2.7 Billing / quota (billing) ---

quota_rejected_total = Counter(
    "lovable_quota_rejected_total",
    "Отказы 402 quota-gate по reason.",
    ["reason"],
)
concurrency_block_by_kind_total = Counter(
    "lovable_concurrency_block_by_kind_total",
    "Отказ старта из-за занятого слота max_concurrent_jobs (TD-012).",
    ["blocked_kind", "holder_kind"],
)
adapty_resync_lag_seconds = Gauge(
    "lovable_adapty_resync_lag_seconds",
    "Максимальный возраст subscriptions.synced_at среди протухших (отставание ресинка).",
)
rate_limit_rejected_total = Counter(
    "lovable_rate_limit_rejected_total",
    "Отказы 429 token-bucket rate-limit (60/min).",
    ["scope"],
)
