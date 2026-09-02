"""Router /templates — каталог шаблонов сайтов (ADR-048).

Публичная витрина для экрана «Templates»: список карточек и их превью-картинки. Шаблон
запускается через `POST /projects` с `template_id` — отдельного эндпоинта старта нет,
чтобы у генерации остался ровно один вход с общими квотами и идемпотентностью.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser
from app.api.errors import not_found, problem_responses
from app.core.config import get_settings
from app.schemas.api import TemplateListResponse, TemplateOut
from app.services import template_service

router = APIRouter(prefix="/templates", tags=["Проекты"])


@router.get(
    "",
    response_model=TemplateListResponse,
    summary="Каталог шаблонов сайтов",
    description=(
        "Возвращает шаблоны для экрана выбора: идентификатор, название и адрес превью. "
        "Порядок элементов — порядок показа карточек. `preview_url` может быть `null` — "
        "картинка ещё не загружена, показывайте свою заглушку. Выбранный шаблон "
        "передаётся в `POST /projects` полем `template_id`. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def list_templates(user: CurrentUser) -> TemplateListResponse:
    """Каталог шаблонов. Одинаков для всех пользователей инстанса."""
    settings = get_settings()
    return TemplateListResponse(
        items=[
            TemplateOut(
                id=template.id,
                title=template.title,
                preview_url=template_service.preview_url(template.id, settings),
            )
            for template in template_service.list_templates()
        ]
    )


@router.get(
    "/{template_id}/preview",
    summary="Превью шаблона",
    description=(
        "Отдаёт превью-картинку шаблона (`image/jpeg`). Неизвестный шаблон или отсутствующая "
        "картинка → `404`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    response_class=Response,
    responses={
        200: {
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
            "description": "Превью-картинка шаблона.",
        },
        **problem_responses(401, 404, 429),
    },
)
async def get_template_preview(template_id: str, user: CurrentUser) -> Response:
    """Превью шаблона. Нет шаблона/картинки → 404."""
    path = template_service.preview_path(template_id)
    if path is None:
        raise not_found("Template preview not found.")
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        # Каталог меняется только с релизом образа — картинку можно смело держать в кэше.
        headers={"Cache-Control": "public, max-age=86400"},
    )
