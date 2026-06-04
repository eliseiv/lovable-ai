"""Unit: per-attempt S3 log-ключи build/deploy/agent (ADR-022, deploy §F-1).

Чистые функции построения ключей — без I/O. Покрывает:
1. формат каждого ключа logs/{job_id}/{build|deploy|agent}.{retry_count}.log;
2. при одном retry_count=N — build.N/deploy.N/agent.N это 3 РАЗНЫХ ключа (нет коллизии);
3. при двух витках (retry 0 и 1) build.0 НЕ затирается build.1 (разные ключи → разные
   объекты в S3).

Нормативный источник: docs/adr/ADR-022-per-attempt-build-logs.md §Decision,
docs/modules/deploy/03-architecture.md §F-1 (таблица стадия↔ключ↔функция).
"""

from __future__ import annotations

import pytest

from app.storage import s3


@pytest.mark.parametrize("retry_count", [0, 1, 2, 7, 42])
def test_build_log_key_format(retry_count: int) -> None:
    jid = "j_abc0000000000000000000"
    assert s3.build_log_key(jid, retry_count) == f"logs/{jid}/build.{retry_count}.log"


@pytest.mark.parametrize("retry_count", [0, 1, 2, 7, 42])
def test_deploy_log_key_format(retry_count: int) -> None:
    jid = "j_abc0000000000000000000"
    assert s3.deploy_log_key(jid, retry_count) == f"logs/{jid}/deploy.{retry_count}.log"


@pytest.mark.parametrize("retry_count", [0, 1, 2, 7, 42])
def test_agent_log_key_format(retry_count: int) -> None:
    jid = "j_abc0000000000000000000"
    assert s3.agent_log_key(jid, retry_count) == f"logs/{jid}/agent.{retry_count}.log"


def test_all_three_keys_share_per_job_prefix() -> None:
    """Все три стадии лежат под единым префиксом logs/{job_id}/ (ретеншн §3 ADR-022:
    подчищаются одним batch-delete по logs/{job_id}/ в project.gc)."""
    jid = "j_prefixcheck0000000000"
    for n in (0, 3):
        for key in (
            s3.build_log_key(jid, n),
            s3.deploy_log_key(jid, n),
            s3.agent_log_key(jid, n),
        ):
            assert key.startswith(f"logs/{jid}/")


# --- A2: один retry_count → 3 РАЗНЫХ ключа (стадии не коллидируют) ---


@pytest.mark.parametrize("retry_count", [0, 1, 5])
def test_three_stages_same_retry_count_are_distinct(retry_count: int) -> None:
    """ADR-022 §Decision: при одном retry_count=N имена build.{N}/deploy.{N}/agent.{N}
    различны → ни одна стадия витка N не затирает лог другой стадии того же витка
    (критично для agent_output_invalid, который пишется тем же N, что build/deploy-фейл)."""
    jid = "j_collision00000000000"
    keys = {
        s3.build_log_key(jid, retry_count),
        s3.deploy_log_key(jid, retry_count),
        s3.agent_log_key(jid, retry_count),
    }
    assert len(keys) == 3, "build/deploy/agent при одном retry_count должны быть 3 разных ключа"


# --- A3: два витка → ранний лог не затирается поздним ---


def test_consecutive_attempts_do_not_collide() -> None:
    """ADR-022 §Decision: разный retry_count → разный ключ. build.0 (виток 0) и build.1
    (виток 1) — разные объекты S3 → пост-мортем причины первого фейла восстановим."""
    jid = "j_twoattempts000000000"
    assert s3.build_log_key(jid, 0) != s3.build_log_key(jid, 1)
    assert s3.deploy_log_key(jid, 0) != s3.deploy_log_key(jid, 1)
    assert s3.agent_log_key(jid, 0) != s3.agent_log_key(jid, 1)


def test_per_attempt_key_does_not_overwrite_prior_attempt_in_store() -> None:
    """Моделируем S3 как dict: запись build.1 НЕ перезаписывает build.0 (прод-инцидент,
    из-за которого вводился ADR-022 — ранний build-лог затирался поздней попыткой)."""
    jid = "j_overwrite0000000000"
    store: dict[str, bytes] = {}
    store[s3.build_log_key(jid, 0)] = b"first build error: bad config"
    store[s3.build_log_key(jid, 1)] = b"second build error: still broken"
    # Оба лога сосуществуют — ранний не затёрт.
    assert store[s3.build_log_key(jid, 0)] == b"first build error: bad config"
    assert store[s3.build_log_key(jid, 1)] == b"second build error: still broken"
    assert len(store) == 2
