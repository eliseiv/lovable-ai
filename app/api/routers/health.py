"""Health/readiness (docs/07-deployment.md → Health / readiness).

/healthz — liveness; /readyz — Postgres + Redis доступны. Без auth.
"""

from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.dependencies import SessionDep
from app.core.config import get_settings
from app.schemas.api import HealthResponse

# Служебные эндпоинты инфраструктуры (liveness/readiness) — НЕ для клиента: скрыты из
# публичной OpenAPI-схемы и Swagger UI (include_in_schema=False, api-contracts §B.5).
router = APIRouter(include_in_schema=False)


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/readyz")
async def readyz(session: SessionDep) -> JSONResponse:
    settings = get_settings()
    checks: dict[str, str] = {}
    ok = True

    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness агрегирует любой сбой
        checks["postgres"] = f"error: {type(exc).__name__}"
        ok = False

    client = aioredis.from_url(settings.redis_url)
    try:
        await client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness агрегирует любой сбой
        checks["redis"] = f"error: {type(exc).__name__}"
        ok = False
    finally:
        await client.aclose()  # type: ignore[attr-defined]  # redis 5.x async; stub устарел

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
