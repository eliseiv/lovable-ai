"""No-progress failure signature (ADR-005, docs/modules/pipeline/03-architecture.md §C(d), §F).

`failure_signature = sha256(failure_class + "\\n" + normalized_core)`, где
`normalized_core` — диагностическое ядро лога с вырезанными нестабильными токенами
(абсолютные пути / таймстампы / PID / hex / `{job_id}`-сегмент). Сигнатура нужна
гарду no-progress: «Agent 4 пропатчил, передеплой дал ту же сигнатуру» (ADR-005).

Сигнатура считается ИМЕННО в pipeline (не в deploy) при получении фейла, до
постановки task_fix. Лог пишет deploy (`build_log_key`, формат — §F): машинно-парсимая
шапка `key: value` (failure_class, exit_code, revision_no, ts), далее сырой stderr.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Машинные классы фейла (docs §F + ADR-005). Класс всегда входит в сигнатуру.
KNOWN_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        "build_error",
        "npm_install_error",
        "deploy_error",
        "health_timeout",
        "health_5xx",
        "health_4xx",
        "agent_output_invalid",
    }
)
_DEFAULT_FAILURE_CLASS = "build_error"

# Маркер начала тела лога: шапка отделена от сырого stderr пустой строкой (см. §F).
_HEADER_BODY_SEP = "\n\n"

# Строки, несущие диагностическое ядро build-fail (ADR-005 п.2): ошибки
# компилятора/сборщика/npm. Регэкспы по нижнему регистру нормализованной строки.
_ERROR_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\berror\b"),
    re.compile(r"\bnpm err!"),
    re.compile(r"cannot find module"),
    re.compile(r"\bfailed\b"),
    re.compile(r"module not found"),
    re.compile(r"exit code"),
    re.compile(r"\bENOENT\b", re.IGNORECASE),
)

# --- Нормализаторы нестабильных токенов (ADR-005 п.3). Порядок важен. ---
# 24-символьные ULID-подобные id (app.core.ids) и прочие длинные hex/base32 → плейсхолдеры.
_RE_ABS_PATH = re.compile(r"(?:/[\w.\-]+)+/?|[a-z]:\\[\\\w.\-]+", re.IGNORECASE)
_RE_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)
_RE_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_RE_NUM = re.compile(r"\b\d+\b")
_RE_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedFailureLog:
    """Распарсенный failure_log: машинный класс из шапки + сырое тело (stderr)."""

    failure_class: str
    body: str
    exit_code: str | None
    revision_no: str | None


def build_failure_log(
    *,
    failure_class: str,
    body: str,
    revision_no: int | None = None,
    exit_code: int | None = None,
    extra_header: dict[str, str] | None = None,
) -> str:
    """Собирает failure_log с машинно-парсимой шапкой (docs §F).

    Шапка — первые строки `key: value` (failure_class, exit_code, revision_no, ts);
    далее пустая строка-разделитель и сырое тело (stderr/детали). Шапка позволяет
    pipeline вычислить failure_class/failure_signature без эвристик по всему телу.
    """
    from datetime import UTC, datetime

    header_lines = [f"failure_class: {failure_class}"]
    if exit_code is not None:
        header_lines.append(f"exit_code: {exit_code}")
    if revision_no is not None:
        header_lines.append(f"revision_no: {revision_no}")
    header_lines.append(f"ts: {datetime.now(UTC).isoformat()}")
    if extra_header:
        for key, value in extra_header.items():
            header_lines.append(f"{key}: {value}")
    return "\n".join(header_lines) + _HEADER_BODY_SEP + body


def parse_failure_log(log: str) -> ParsedFailureLog:
    """Парсит шапку failure_log → failure_class/exit_code/revision_no + тело.

    Шапка устойчива к отсутствию полей; неизвестный/пустой failure_class → дефолт.
    """
    header_part, _, body = log.partition(_HEADER_BODY_SEP)
    header: dict[str, str] = {}
    for line in header_part.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            header[key.strip().lower()] = value.strip()
    failure_class = header.get("failure_class", "")
    if failure_class not in KNOWN_FAILURE_CLASSES:
        # Тело и шапка могли слипнуться (нет разделителя) — тогда body пуст.
        failure_class = _DEFAULT_FAILURE_CLASS if failure_class == "" else failure_class
    return ParsedFailureLog(
        failure_class=failure_class,
        body=body if body else header_part,
        exit_code=header.get("exit_code"),
        revision_no=header.get("revision_no"),
    )


def _normalize_token(line: str) -> str:
    """Вырезает нестабильные токены строки, приводит к нижнему регистру, схлопывает пробелы."""
    out = line.lower()
    out = _RE_TIMESTAMP.sub("<ts>", out)
    out = _RE_ABS_PATH.sub("<path>", out)
    out = _RE_HEX.sub("<hex>", out)
    out = _RE_NUM.sub("<num>", out)
    return _RE_WS.sub(" ", out).strip()


def _diagnostic_core(body: str) -> str:
    """Извлекает диагностическое ядро (ADR-005 п.2) и нормализует (п.3).

    build-fail/npm: error-строки сборщика; health-fail: класс ответа health-check;
    invalid-agent-output: машинный код правила (тело несёт его как одну строку).
    Множество error-строк сортируется (порядок недетерминирован между прогонами).
    """
    error_lines: set[str] = set()
    for raw_line in body.splitlines():
        normalized = _normalize_token(raw_line)
        if not normalized:
            continue
        if any(pat.search(normalized) for pat in _ERROR_LINE_PATTERNS):
            error_lines.add(normalized)

    if not error_lines:
        # Нет явных error-строк (health-fail/invalid-output несут компактное тело):
        # берём всё нормализованное тело как ядро — оно уже компактно.
        return _normalize_token(" ".join(body.splitlines()))

    return "\n".join(sorted(error_lines))


def compute_failure_signature(log: str) -> str:
    """Считает failure_signature по failure_log (ADR-005).

    Детерминированный sha256(failure_class + "\\n" + normalized_core), hex.
    """
    parsed = parse_failure_log(log)
    core = _diagnostic_core(parsed.body)
    digest = hashlib.sha256(f"{parsed.failure_class}\n{core}".encode())
    return digest.hexdigest()
