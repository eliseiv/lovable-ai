"""Unit (Sprint 6, ADR-015): нормативная таблица метрик lovable_* + unbounded-label guard.

docs/modules/observability/03-architecture.md §1/§2, docs/06-testing-strategy §6.

Проверяет:
  - все 34 метрики §2 присутствуют в дефолтном REGISTRY под именами/типами таблицы;
  - guard: НИ ОДНА lovable_*-метрика не несёт label job_id/user_id/subdomain/apns_token
    (запрет unbounded-labels §1 — взрыв кардинальности; эти идентификаторы → Sentry/логи);
  - render_latest() отдаёт prometheus text exposition с lovable_*-семействами;
  - model-tiering дефолты config: AGENT1/AGENT4 = Sonnet, AGENT2/AGENT3 = Opus (§5.3).

Внешних границ нет (чистый registry + Settings) — unit, без Postgres/Redis/Claude.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from app.core.config import Settings

# Нормативная таблица §2 (имя → ожидаемый prometheus-type). Counter-семейства в REGISTRY
# регистрируются под базовым именем без суффикса _total (prometheus_client добавляет _total
# в exposition сам), поэтому ключ — имя БЕЗ _total для counter'ов.
# (имя_метрики_в_registry, type, ожидаемые_labels)
_EXPECTED: list[tuple[str, str, frozenset[str]]] = [
    # 2.1 Pipeline / jobs
    ("lovable_jobs", "counter", frozenset({"kind", "terminal_state"})),
    ("lovable_jobs_in_state", "gauge", frozenset({"state", "kind"})),
    ("lovable_job_failed", "counter", frozenset({"reason", "kind"})),
    ("lovable_build_duration_seconds", "histogram", frozenset({"result"})),
    ("lovable_fix_loop_depth", "histogram", frozenset({"terminal_state"})),
    ("lovable_no_progress_trips", "counter", frozenset()),
    # 2.2 Cost / LLM
    ("lovable_job_cost_usd", "histogram", frozenset({"kind", "terminal_state"})),
    ("lovable_llm_call_cost_usd", "counter", frozenset({"agent", "model"})),
    ("lovable_llm_tokens", "counter", frozenset({"agent", "model", "token_type"})),
    ("lovable_llm_cache_hit_ratio", "gauge", frozenset({"agent"})),
    ("lovable_llm_call_latency_seconds", "histogram", frozenset({"agent", "model"})),
    ("lovable_user_spend_usd", "gauge", frozenset()),
    # 2.3 SSE / realtime
    ("lovable_sse_streams_open", "gauge", frozenset()),
    ("lovable_sse_stream_duration_seconds", "histogram", frozenset({"close_reason"})),
    ("lovable_sse_rejected", "counter", frozenset({"reason"})),
    ("lovable_sse_heartbeat_catchup", "counter", frozenset({"result"})),
    # 2.4 APNs
    ("lovable_apns_push", "counter", frozenset({"result", "apns_status"})),
    ("lovable_apns_tokens_invalidated", "counter", frozenset({"reason"})),
    ("lovable_apns_request_latency_seconds", "histogram", frozenset()),
    # 2.5 Edit / rollback / deploy
    ("lovable_edit_outcome", "counter", frozenset({"outcome"})),
    ("lovable_rollback", "counter", frozenset({"trigger", "result"})),
    ("lovable_redeploy_duration_seconds", "histogram", frozenset({"kind"})),
    ("lovable_dist_artifact_source", "counter", frozenset({"source"})),
    ("lovable_project_gc_pending", "gauge", frozenset()),
    ("lovable_project_gc_duration_seconds", "histogram", frozenset({"result"})),
    # 2.6 Queue / worker
    ("lovable_queue_depth", "gauge", frozenset({"queue"})),
    ("lovable_worker_busy", "gauge", frozenset({"queue"})),
    ("lovable_redis_pool_in_use", "gauge", frozenset({"pool"})),
    ("lovable_billing_resync_batch", "histogram", frozenset({"result"})),
    # 2.7 Billing / quota
    ("lovable_quota_rejected", "counter", frozenset({"reason"})),
    ("lovable_concurrency_block_by_kind", "counter", frozenset({"blocked_kind", "holder_kind"})),
    ("lovable_adapty_resync_lag_seconds", "gauge", frozenset()),
    ("lovable_rate_limit_rejected", "counter", frozenset({"scope"})),
]

# Запрещённые unbounded-labels (§1, ADR-015 §3) — ни одна lovable_*-метрика не должна их нести.
_FORBIDDEN_LABELS = frozenset({"job_id", "user_id", "subdomain", "apns_token"})


def _collect_lovable_families() -> dict[str, object]:
    """Снимок lovable_*-семейств REGISTRY: {family_name: metric_family}."""
    import app.observability.metrics  # noqa: F401 — гарантирует регистрацию метрик в REGISTRY

    families: dict[str, object] = {}
    for metric in REGISTRY.collect():
        if metric.name.startswith("lovable_"):
            families[metric.name] = metric
    return families


def test_all_metrics_present_with_names_and_types():
    """Нормативная таблица §2: все lovable_*-метрики зарегистрированы с верным типом.

    Каноничный счёт по §2 (2.1..2.7) = 33 (6+6+4+3+6+4+4); ровно столько определяет и
    прод-код app/observability/metrics.py. ТЗ упоминало «34» — расхождение на единицу с
    реальной нормативной таблицей §2 (см. prompt_issues); тест сверяется с фактическим
    REGISTRY/таблицей, а не с числом из формулировки задачи.
    """
    families = _collect_lovable_families()
    assert len(_EXPECTED) == 33, "нормативная таблица §2 содержит 33 метрики (2.1..2.7)"
    # Все 33 описанные метрики должны быть в живом REGISTRY и ровно столько lovable_*-семейств.
    missing = [name for name, _, _ in _EXPECTED if name not in families]
    assert not missing, f"отсутствуют метрики из §2: {missing}"
    extra = sorted(set(families) - {name for name, _, _ in _EXPECTED})
    assert not extra, f"в REGISTRY есть незадекларированные lovable_*-метрики: {extra}"
    for name, expected_type, _labels in _EXPECTED:
        assert families[name].type == expected_type, (  # type: ignore[attr-defined]
            f"{name}: тип {families[name].type!r} != ожидаемого {expected_type!r}"  # type: ignore[attr-defined]
        )


def test_metric_labels_match_normative_table():
    """Labels каждой метрики совпадают с §2 символ-в-символ (через label-наборы образцов)."""
    import app.observability.metrics as m

    # Карта registry-имя → объект метрики (для чтения _labelnames без сэмплов).
    obj_by_name = {
        "lovable_jobs": m.jobs_total,
        "lovable_jobs_in_state": m.jobs_in_state,
        "lovable_job_failed": m.job_failed_total,
        "lovable_build_duration_seconds": m.build_duration_seconds,
        "lovable_fix_loop_depth": m.fix_loop_depth,
        "lovable_no_progress_trips": m.no_progress_trips_total,
        "lovable_job_cost_usd": m.job_cost_usd,
        "lovable_llm_call_cost_usd": m.llm_call_cost_usd_total,
        "lovable_llm_tokens": m.llm_tokens_total,
        "lovable_llm_cache_hit_ratio": m.llm_cache_hit_ratio,
        "lovable_llm_call_latency_seconds": m.llm_call_latency_seconds,
        "lovable_user_spend_usd": m.user_spend_usd,
        "lovable_sse_streams_open": m.sse_streams_open,
        "lovable_sse_stream_duration_seconds": m.sse_stream_duration_seconds,
        "lovable_sse_rejected": m.sse_rejected_total,
        "lovable_sse_heartbeat_catchup": m.sse_heartbeat_catchup_total,
        "lovable_apns_push": m.apns_push_total,
        "lovable_apns_tokens_invalidated": m.apns_tokens_invalidated_total,
        "lovable_apns_request_latency_seconds": m.apns_request_latency_seconds,
        "lovable_edit_outcome": m.edit_outcome_total,
        "lovable_rollback": m.rollback_total,
        "lovable_redeploy_duration_seconds": m.redeploy_duration_seconds,
        "lovable_dist_artifact_source": m.dist_artifact_source_total,
        "lovable_project_gc_pending": m.project_gc_pending,
        "lovable_project_gc_duration_seconds": m.project_gc_duration_seconds,
        "lovable_queue_depth": m.queue_depth,
        "lovable_worker_busy": m.worker_busy,
        "lovable_redis_pool_in_use": m.redis_pool_in_use,
        "lovable_billing_resync_batch": m.billing_resync_batch,
        "lovable_quota_rejected": m.quota_rejected_total,
        "lovable_concurrency_block_by_kind": m.concurrency_block_by_kind_total,
        "lovable_adapty_resync_lag_seconds": m.adapty_resync_lag_seconds,
        "lovable_rate_limit_rejected": m.rate_limit_rejected_total,
    }
    for name, _type, expected_labels in _EXPECTED:
        actual = frozenset(obj_by_name[name]._labelnames)
        assert actual == expected_labels, f"{name}: labels {actual} != {expected_labels}"


@pytest.mark.parametrize("forbidden", sorted(_FORBIDDEN_LABELS))
def test_no_metric_carries_unbounded_label(forbidden: str):
    """GUARD §1: ни одна lovable_*-метрика не несёт unbounded-label (взрыв кардинальности)."""
    for name, _type, labels in _EXPECTED:
        assert forbidden not in labels, (
            f"метрика {name} несёт запрещённый unbounded-label {forbidden!r} "
            f"(должен быть в Sentry/логах, не в Prometheus — ADR-015 §1)"
        )


def test_no_unbounded_label_in_live_registry():
    """GUARD §1 на живом registry: ни в одном lovable_*-семействе нет запрещённого labelname."""
    import app.observability.metrics as m

    all_objs = [
        m.jobs_total,
        m.jobs_in_state,
        m.job_failed_total,
        m.build_duration_seconds,
        m.fix_loop_depth,
        m.no_progress_trips_total,
        m.job_cost_usd,
        m.llm_call_cost_usd_total,
        m.llm_tokens_total,
        m.llm_cache_hit_ratio,
        m.llm_call_latency_seconds,
        m.user_spend_usd,
        m.sse_streams_open,
        m.sse_stream_duration_seconds,
        m.sse_rejected_total,
        m.sse_heartbeat_catchup_total,
        m.apns_push_total,
        m.apns_tokens_invalidated_total,
        m.apns_request_latency_seconds,
        m.edit_outcome_total,
        m.rollback_total,
        m.redeploy_duration_seconds,
        m.dist_artifact_source_total,
        m.project_gc_pending,
        m.project_gc_duration_seconds,
        m.queue_depth,
        m.worker_busy,
        m.redis_pool_in_use,
        m.billing_resync_batch,
        m.quota_rejected_total,
        m.concurrency_block_by_kind_total,
        m.adapty_resync_lag_seconds,
        m.rate_limit_rejected_total,
    ]
    for obj in all_objs:
        labelnames = frozenset(obj._labelnames)
        leaked = labelnames & _FORBIDDEN_LABELS
        assert not leaked, f"{obj._name}: unbounded-label leak {leaked}"


def test_counter_names_end_with_total_in_exposition():
    """Counter-имена в exposition оканчиваются на _total (конвенция Prometheus §2)."""
    from app.observability.exposition import render_latest

    body, content_type = render_latest()
    text = body.decode()
    assert "text/plain" in content_type
    # Несколько counter'ов из §2 — их _total-форма обязана присутствовать в выводе.
    for counter_name in (
        "lovable_jobs_total",
        "lovable_job_failed_total",
        "lovable_apns_push_total",
        "lovable_quota_rejected_total",
    ):
        assert counter_name in text, f"{counter_name} отсутствует в exposition"


def test_render_latest_prometheus_content_type():
    """render_latest() возвращает корректный prometheus text-exposition content-type."""
    from prometheus_client import CONTENT_TYPE_LATEST

    from app.observability.exposition import render_latest

    body, content_type = render_latest()
    assert content_type == CONTENT_TYPE_LATEST
    assert isinstance(body, bytes)


# --- Model tiering (§5.3, 08 §6-2) — дефолты config символ-в-символ ---


def test_model_tiering_defaults():
    """Дефолты AGENTn_MODEL (ADR-023 §Decision (3), ревизия R1): только Agent 2 (Spec) = Opus;
    Agent 1 (Interviewer) / Agent 3 (Builder) / Agent 4 (Fixer) = Sonnet.

    Источник истины — docs/modules/pipeline/03-architecture.md §Агенты → Tiering моделей (R1:
    Builder переведён Opus→Sonnet ради стоимости). Прежний дефолт agent3=Opus устарел.
    """
    s = Settings(
        database_url="postgresql+asyncpg://x:y@127.0.0.1/db",
        redis_url="redis://127.0.0.1:6379/0",
        seed_api_key="k",
        anthropic_api_key="k",
    )
    assert s.agent1_model == "claude-sonnet-4-6"
    assert s.agent2_model == "claude-opus-4-8"
    assert s.agent3_model == "claude-sonnet-4-6"  # R1: Builder Opus→Sonnet (ADR-023)
    assert s.agent4_model == "claude-sonnet-4-6"
