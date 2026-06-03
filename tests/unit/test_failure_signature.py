"""Unit: failure_signature нормализация и детерминизм (ADR-005, docs §C(d)/§F).

Сигнатура = sha256(failure_class + "\\n" + normalized_core). Требования ADR-005:
- ДЕТЕРМИНИЗМ: один и тот же фейл (та же причина) при разных нестабильных токенах
  (абсолютные пути / таймстампы / PID / job_id / hex) даёт ОДНУ сигнатуру;
- РАЗЛИЧИМОСТЬ: две разные причины (разный failure_class или разное ядро) — разные
  сигнатуры;
- парсинг машинной шапки (§F): failure_class из `key: value`-шапки.

Без сети/БД — чистые функции над строками логов (build_error / npm / health /
agent_output_invalid / deploy_error).
"""

from __future__ import annotations

from app.pipeline.failure_signature import (
    build_failure_log,
    compute_failure_signature,
    parse_failure_log,
)

# --- фикстуры логов: одна причина, два разных «зашумлённых» прогона ---


def _build_error_log(job_seg: str, ts: str, line_no: int) -> str:
    """build-fail: TS-ошибка с разными путём/таймстампом/номером строки/job_id."""
    body = (
        f"{ts} starting vite build\n"
        f"/var/builds/{job_seg}/src/main.ts:{line_no}:10 - "
        f"error TS2304: Cannot find module './missing'\n"
        f"exit code 2\n"
    )
    return build_failure_log(failure_class="build_error", body=body, revision_no=1, exit_code=2)


def _npm_error_log(job_seg: str) -> str:
    body = (
        f"npm ERR! code ENOENT\n"
        f"npm ERR! path /var/builds/{job_seg}/package.json\n"
        f"npm ERR! enoent ENOENT: no such file or directory\n"
    )
    return build_failure_log(failure_class="npm_install_error", body=body, exit_code=1)


# --- ДЕТЕРМИНИЗМ: та же причина, разный шум → одна сигнатура ---


def test_build_error_signature_stable_across_paths_ts_pid():
    """ADR-005: разные абсолютные пути/таймстампы/job_id/номера → ОДНА сигнатура."""
    sig_a = compute_failure_signature(_build_error_log("jobAAA111", "2026-06-02T10:00:00Z", 12))
    sig_b = compute_failure_signature(_build_error_log("jobBBB999", "2025-01-01T23:59:59Z", 47))
    assert sig_a == sig_b
    assert len(sig_a) == 64  # sha256 hex


def test_signature_is_deterministic_same_input():
    log = _build_error_log("jobX", "2026-06-02T10:00:00Z", 3)
    assert compute_failure_signature(log) == compute_failure_signature(log)


def test_npm_error_signature_stable_across_paths():
    sig_a = compute_failure_signature(_npm_error_log("jobAAA"))
    sig_b = compute_failure_signature(_npm_error_log("jobZZZ"))
    assert sig_a == sig_b


def test_error_line_order_does_not_affect_signature():
    """ADR-005 п.3: множество error-строк сортируется — порядок не влияет."""
    body1 = "error TS1: bad\nerror TS2: worse\n"
    body2 = "error TS2: worse\nerror TS1: bad\n"
    log1 = build_failure_log(failure_class="build_error", body=body1)
    log2 = build_failure_log(failure_class="build_error", body=body2)
    assert compute_failure_signature(log1) == compute_failure_signature(log2)


# --- РАЗЛИЧИМОСТЬ: разная причина → разные сигнатуры ---


def test_different_error_core_distinct_signature():
    log_a = _build_error_log("j", "2026-06-02T10:00:00Z", 1)
    log_other = build_failure_log(
        failure_class="build_error",
        body="error TS5055: Cannot write file 'dist/x' because it would overwrite\n",
    )
    assert compute_failure_signature(log_a) != compute_failure_signature(log_other)


def test_different_failure_class_distinct_signature():
    """failure_class всегда входит в сигнатуру (ADR-005 п.1): build vs npm → разные."""
    body = "error something failed\n"
    build = build_failure_log(failure_class="build_error", body=body)
    npm = build_failure_log(failure_class="npm_install_error", body=body)
    assert compute_failure_signature(build) != compute_failure_signature(npm)


def test_health_vs_deploy_distinct_signature():
    health = build_failure_log(failure_class="health_timeout", body="health check failed: timeout")
    deploy = build_failure_log(
        failure_class="deploy_error", body="nginx container failed to start: boom"
    )
    assert compute_failure_signature(health) != compute_failure_signature(deploy)


def test_health_5xx_vs_4xx_distinct_signature():
    h5 = build_failure_log(failure_class="health_5xx", body="status 502 bad gateway")
    h4 = build_failure_log(failure_class="health_4xx", body="status 404 not found")
    assert compute_failure_signature(h5) != compute_failure_signature(h4)


def test_agent_output_invalid_signature_by_rule():
    """invalid-agent-output: разные машинные коды правила → разные сигнатуры."""
    traversal = build_failure_log(
        failure_class="agent_output_invalid",
        body="agent4 patch rejected: ... (rule=path_traversal)",
    )
    too_large = build_failure_log(
        failure_class="agent_output_invalid",
        body="agent4 patch rejected: ... (rule=tree_too_large)",
    )
    assert compute_failure_signature(traversal) != compute_failure_signature(too_large)


def test_health_timeout_stable_across_durations():
    """health_timeout: разная длительность ожидания (bare-число) → одна сигнатура.

    Нормализатор <NUM> (ADR-005 п.3) вырезает отдельно стоящие числа. Длительности с
    приклеенной единицей (`60s`) — нормализуются неполно: осознанная граница ADR-005
    (TD-005, Sprint 6 — донастройка нормализаторов под реальные логи), поэтому здесь
    проверяется bare-число (порт/PID/длительность как отдельный токен).
    """
    a = build_failure_log(
        failure_class="health_timeout", body="health check failed: timeout after 60 seconds"
    )
    b = build_failure_log(
        failure_class="health_timeout", body="health check failed: timeout after 73 seconds"
    )
    assert compute_failure_signature(a) == compute_failure_signature(b)


# --- парсинг машинной шапки (§F) ---


def test_parse_header_extracts_failure_class_and_fields():
    log = build_failure_log(
        failure_class="build_error", body="some stderr", revision_no=4, exit_code=2
    )
    parsed = parse_failure_log(log)
    assert parsed.failure_class == "build_error"
    assert parsed.exit_code == "2"
    assert parsed.revision_no == "4"
    assert "some stderr" in parsed.body


def test_parse_unknown_failure_class_falls_back():
    """Неизвестный класс при отсутствии — дефолт build_error (устойчивость парсера)."""
    parsed = parse_failure_log("no header here\njust body")
    assert parsed.failure_class == "build_error"


def test_parse_known_unusual_class_preserved():
    log = build_failure_log(failure_class="agent_output_invalid", body="x")
    assert parse_failure_log(log).failure_class == "agent_output_invalid"
