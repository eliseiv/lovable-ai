"""Экспозиция /metrics (Sprint 6, ADR-015, docs observability §1).

Двойная экспозиция (два процесса разной природы):
  - FastAPI app — точный bare-Route GET /metrics (registry процесса), internal-route (не под /v1,
    не публичный — только cluster/compose-scrape). См. metrics_endpoint (регистрируется как
    точный Route в app.api.main, НЕ mount — observability §1: Mount даёт 307/404 на bare-пути).
  - Celery worker/beat — start_http_server(METRICS_PORT) в worker-процессе (отдельный HTTP-порт;
    у воркера нет ASGI). См. start_worker_metrics_server (вызывается по worker_process_init).

Multiprocess-режим app (PROMETHEUS_MULTIPROC_DIR): если app запускается несколькими uvicorn-
процессами — registry собирается из multiproc-каталога. Если один процесс на реплику
(рекомендация §1) — дефолтный REGISTRY. Выбор фиксируется devops в compose; здесь —
автоопределение по наличию env PROMETHEUS_MULTIPROC_DIR.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = get_logger(__name__)


def render_latest() -> tuple[bytes, str]:
    """Сериализует текущий снимок метрик в exposition-формат Prometheus.

    Возвращает (body, content_type). Учитывает multiprocess-режим: при заданном
    PROMETHEUS_MULTIPROC_DIR собирает registry из multiproc-каталога (иначе — дефолтный).
    """
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    settings = get_settings()
    if settings.prometheus_multiproc_dir:
        from prometheus_client import CollectorRegistry, multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST


async def metrics_endpoint(request: Request) -> Response:
    """Точный bare-Route GET /metrics (регистрируется в app.api.main как Route, НЕ mount).

    Делегирует в render_latest() — registry процесса целиком. Объявлен точным путём `/metrics`
    (а не префиксным Mount), поэтому матчит ровно `/metrics` без trailing-slash-канонизации и
    отдаёт 200 prometheus-text напрямую, без 307/308 (observability §1, инвариант I1). Только GET
    (methods=["GET"] на Route) → прочие методы дают 405 средствами Starlette-роутера.
    """
    from starlette.responses import Response

    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


def start_worker_metrics_server() -> None:
    """Поднимает HTTP-сервер метрик воркера/beat на METRICS_PORT (start_http_server).

    Вызывается по сигналу Celery worker_process_init (один сервер на worker-процесс).
    Идемпотентность на уровне процесса не гарантируется prometheus_client — вызывать ровно
    один раз на процесс (через signal). Ошибка bind (порт занят) логируется, не валит воркер.
    """
    from prometheus_client import start_http_server

    settings = get_settings()
    try:
        start_http_server(settings.metrics_port)
        logger.info("worker_metrics_server_started", extra={"port": settings.metrics_port})
    except OSError as exc:
        logger.warning(
            "worker_metrics_server_bind_failed",
            extra={"port": settings.metrics_port, "error": str(exc)},
        )
