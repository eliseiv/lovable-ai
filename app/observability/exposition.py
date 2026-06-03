"""Экспозиция /metrics (Sprint 6, ADR-015, docs observability §1).

Двойная экспозиция (два процесса разной природы):
  - FastAPI app — ASGI-эндпоинт GET /metrics (registry процесса), internal-route (не под /v1,
    не публичный — только cluster/compose-scrape). См. metrics_endpoint / mount в app.api.main.
  - Celery worker/beat — start_http_server(METRICS_PORT) в worker-процессе (отдельный HTTP-порт;
    у воркера нет ASGI). См. start_worker_metrics_server (вызывается по worker_process_init).

Multiprocess-режим app (PROMETHEUS_MULTIPROC_DIR): если app запускается несколькими uvicorn-
процессами — registry собирается из multiproc-каталога. Если один процесс на реплику
(рекомендация §1) — дефолтный REGISTRY. Выбор фиксируется devops в compose; здесь —
автоопределение по наличию env PROMETHEUS_MULTIPROC_DIR.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ASGI-типы (совместимы со Starlette mount-сигнатурой).
_Scope = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
_Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


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


async def metrics_asgi_app(scope: _Scope, receive: _Receive, send: _Send) -> None:
    """Минимальное ASGI-приложение GET /metrics (монтируется в FastAPI как internal-route).

    Не зависит от FastAPI-роутинга (отдельный ASGI mount) — registry процесса целиком, без
    обхода через response_model. Только GET; прочее → 405.
    """
    if scope["type"] != "http":
        return
    if scope.get("method") != "GET":
        await _send_response(send, 405, b"method not allowed", "text/plain")
        return
    body, content_type = render_latest()
    await _send_response(send, 200, body, content_type)


async def _send_response(send: _Send, status: int, body: bytes, content_type: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode("latin-1"))],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
