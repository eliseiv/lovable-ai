"""Observability (Sprint 6, ADR-015/ADR-016): Prometheus-метрики, Sentry, Redis pool.

Нормативный контракт — docs/modules/observability/03-architecture.md. Этот пакет:
  - metrics: нормативная таблица lovable_* (§2), экспозиция /metrics (§1);
  - sentry: init FastAPI+Celery, before_send scrubbing секретов (§4);
  - redis_pool: переиспользуемый ConnectionPool/синглтон-клиент (TD-007, §6).

PROMETHEUS_MULTIPROC_DIR — нормализация наличия пустого ключа (см. _normalize_multiproc_env
ниже). Выполняется на импорте пакета — ДО любого import app.observability.metrics, т.е. до
создания первого Counter/Gauge/Histogram (Python исполняет __init__ пакета раньше его
submodule'ов).
"""

from __future__ import annotations

import os


def _normalize_multiproc_env() -> None:
    """Убирает пустой/whitespace-only PROMETHEUS_MULTIPROC_DIR из os.environ.

    prometheus_client активирует multiprocess-режим по самому НАЛИЧИЮ ключа
    'PROMETHEUS_MULTIPROC_DIR' в os.environ (prometheus_client/values.py, metrics.py —
    проверка `... in os.environ`, не значения). docker-compose прокидывает
    `PROMETHEUS_MULTIPROC_DIR=${PROMETHEUS_MULTIPROC_DIR:-}` — ключ ПРИСУТСТВУЕТ со значением
    "" (пустая строка). Это ошибочно включает multiproc → prometheus_client пишет
    counter_*.db в cwd /app (read-only для non-root UID) → PermissionError → api не стартует.

    По docs/07 дефолт PROMETHEUS_MULTIPROC_DIR пустой = режим «один процесс на реплику»
    (multiproc НЕ активируется). Поэтому при отсутствии/пустом/whitespace-only значении
    удаляем ключ целиком — prometheus_client остаётся в single-process режиме (дефолтный
    REGISTRY). Непустой путь сохраняется как есть → multiproc-режим (worker/beat по docs).

    Идемпотентно. Должно выполниться ДО первого создания метрики (гарантируется импортом
    пакета раньше app.observability.metrics).
    """
    value = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if value is not None and not value.strip():
        os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)


_normalize_multiproc_env()
