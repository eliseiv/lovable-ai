"""FastAPI-приложение (entrypoint: uvicorn app.api.main:app, docs/07-deployment.md).

Версионирование пути /v1. RFC-7807 ошибки. API не трогает Docker/Claude —
только Postgres + Redis (docs/01-architecture.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi

from app.api.errors import (
    ProblemException,
    problem_exception_handler,
    validation_exception_handler,
)
from app.api.routers import auth, billing, devices, health, jobs, projects
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.observability import sentry
from app.observability.exposition import metrics_endpoint
from app.observability.redis_pool import close_pool

settings = get_settings()
configure_logging(settings.log_level)
# Sprint 6 (ADR-015): Sentry init FastAPI+Celery. Пустой SENTRY_DSN → no-op (процесс цел).
sentry.init_sentry(settings)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Lifespan: освобождение переиспользуемого Redis-пула соединений на остановке."""
    yield
    await close_pool()


# Публичная вводная для iOS-разработчика (B.6): что делает API, как авторизоваться, что
# генерация асинхронная, формат ошибок. Без внутренних маркеров (denylist B.2).
_API_DESCRIPTION = """\
API генерации сайтов по текстовому описанию.

Сервис превращает текстовый промт в готовый веб-сайт и публикует его по адресу `live_url`.
Перед генерацией сервис может задать уточняющие вопросы, чтобы точнее понять задачу.

### Авторизация
Все запросы (кроме входа через Apple и приёма событий магазина подписок) требуют заголовок
`Authorization: Bearer <api-key>`. Ключ выдаётся методом `POST /auth/apple` после входа через
Apple и имеет формат `lv_<key_id>_<secret>`.

### Асинхронность
Создание сайта и правки выполняются асинхронно: в ответ приходит идентификатор задачи
(`job_id`). Текущий статус получают опросом `GET /jobs/{jid}` либо подпиской на поток событий
`GET /jobs/{jid}/events` (Server-Sent Events).

### Формат ошибок
Все ошибки возвращаются в формате RFC 7807 (`application/problem+json`) с полями `type`,
`title`, `status`, `detail` и доменными полями (`reason`, `required_entitlement`) где применимо.

### Лимиты
Действует ограничение частоты запросов — 60 запросов в минуту на ключ; при превышении
возвращается код `429`.
"""

# Доменные русские tags (B.4). Имена и состав — нормативная таблица api-contracts §B.4;
# внутренняя модульная принадлежность наружу не выносится (группировка чисто доменная).
_OPENAPI_TAGS = [
    {
        "name": "Аутентификация",
        "description": "Вход через Apple и управление токенами устройств.",
    },
    {
        "name": "Проекты",
        "description": "Создание сайтов, список, детали, удаление.",
    },
    {
        "name": "Джобы генерации",
        "description": "Статус генерации, уточняющие вопросы, ответы, live-поток событий.",
    },
    {
        "name": "Правки и ревизии",
        "description": "Post-delivery правки сайта, история ревизий, откат.",
    },
    {
        "name": "Устройства",
        "description": "Регистрация и отписка устройств для push-уведомлений.",
    },
    {
        "name": "Биллинг",
        "description": "Тариф, остаток квоты, приём событий магазина подписок.",
    },
]


app = FastAPI(
    title="API генерации сайтов",
    version="1.0.0",
    description=_API_DESCRIPTION,
    openapi_tags=_OPENAPI_TAGS,
    # Базовый URL = ОРИГИН текущего инстанса БЕЗ суффикса (доменные роутеры уже несут
    # префикс /v1, поэтому путь в схеме = `/v1/...`; добавлять `/v1` в server.url нельзя —
    # иначе Swagger «Try it out» бьёт в `/v1/v1/...`). Берётся из APPS_DOMAIN, поэтому на
    # каждом инстансе указывает на СВОЙ хост (corelysite.shop / nexoraweb.shop) → запрос
    # из Swagger идёт same-origin (нет cross-origin/CORS «TypeError: Load failed»).
    servers=[{"url": f"https://{settings.apps_domain}", "description": "Текущий инстанс"}],
    lifespan=_lifespan,
)

# RFC-7807 для ВСЕХ ошибок (docs/modules/api/03 → Обработчики ошибок). ProblemException —
# доменные 401/402/404/409/429/422. RequestValidationError — app-level handler нормализует
# дефолтный FastAPI-422 ({detail:[...]}) в application/problem+json для ВСЕХ эндпоинтов,
# включая публичный `POST /auth/apple` без `identity_token` (прод-фикс 2026-06-04, auth/02 §Ошибки).
app.add_exception_handler(ProblemException, problem_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)


@app.middleware("http")
async def _sentry_correlation(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Изолированный Sentry-scope на запрос для correlation-тегов (Sprint 6, ADR-015 §4).

    Контракт §4: исключение ТЕКУЩЕГО запроса должно нести correlation-теги (user_id и пр.).
    Тег `sentry_sdk.set_tag` действует на АКТИВНЫЙ scope для последующих событий — поэтому
    тегировать после `call_next` поздно (исключение запроса уже ушло в Sentry без тега, а тег
    утёк бы на следующее событие). Решение: открыть изолированный scope ДО обработки и держать
    его активным на всё время `call_next` — auth-dependency проставит `user_id` в этот же
    активный scope (см. `get_current_user`) до тела эндпоинта, и любое исключение внутри
    `call_next` будет захвачено с тегом. `isolation_scope` гарантирует, что теги одного запроса
    не протекают в соседние (важно под общим event-loop). No-op без Sentry.

    job_id/project_id — высококардинальные теги тасок/воркеров, проставляются точечно в
    Celery-обёртке/обработчиках в scope ДО возможного исключения (вне этого middleware).
    """
    with sentry.request_scope():
        return await call_next(request)


# Prometheus /metrics — точный bare-Route (Sprint 6, ADR-015 §1, прод-фикс 2026-06-04 раунд 2):
# не под /v1, не публичный (только cluster/compose-scrape, наружу через Traefik не публикуется),
# registry процесса. Объявлен ТОЧНЫМ путём `/metrics` (Starlette/FastAPI Route, GET,
# include_in_schema=False), НЕ `app.mount`: Mount — префиксное под-приложение, обслуживает bare
# `/metrics` только через trailing-slash-редирект (307 → scheme-downgrade за TLS-прокси при
# redirect_slashes=True, либо 404 при redirect_slashes=False — обе регрессии нарушают
# scrape-инвариант I1, observability §1). Точный Route матчит ровно `/metrics` без
# slash-канонизации → 200 prometheus-text напрямую, без 307/308, независимо от глобального
# redirect_slashes. `GET /v1/metrics` → 404 (роут вне /v1-префикса). docs/07 §Proxy-headers (1).
app.add_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

# Health — без префикса (liveness/readiness probes).
app.include_router(health.router)

# Версионированные доменные роутеры под /v1.
app.include_router(auth.router, prefix="/v1")
app.include_router(projects.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")
app.include_router(billing.router, prefix="/v1")
# Sprint 5: регистрация APNs устройств (ADR-013).
app.include_router(devices.router, prefix="/v1")


def _custom_openapi() -> dict[str, Any]:
    """OpenAPI с HTTP-Bearer security-схемой (B.6).

    FastAPI не выводит securityScheme автоматически (auth — кастомная dependency, не
    `Security(...)`), поэтому кнопка Authorize в Swagger UI ничего не привязывала к запросам.
    Объявляем `BearerAuth` (http/bearer) и глобальное `security`, чтобы введённый в Authorize
    ключ уходил как `Authorization: Bearer <api-key>` на «Try it out». Эндпоинты без Bearer
    (`/auth/apple`, webhook) принимают лишний заголовок безвредно.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=_OPENAPI_TAGS,
        servers=app.servers,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "API-ключ `lv_<key_id>_<secret>` из `POST /v1/auth/apple`.",
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]
