"""Router /styles — каталог визуальных стилей сайта (ADR-050).

Витрина для экрана выбора оформления: список карточек и их превью. Стиль применяется через
`POST /projects` полем `style_id` — как и шаблон, отдельного входа в генерацию у него нет.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser
from app.api.errors import not_found, problem_responses
from app.core.config import get_settings
from app.schemas.api import StyleListResponse, StyleOut
from app.services import style_service

router = APIRouter(prefix="/styles", tags=["Проекты"])


@router.get(
    "",
    response_model=StyleListResponse,
    summary="Каталог визуальных стилей",
    description=(
        "Возвращает стили оформления для экрана выбора: идентификатор, название и адрес "
        "превью. Стиль задаёт внешний вид сайта (типографика, цвет, тени, иконки) и не "
        "влияет на состав секций. Порядок элементов — порядок показа карточек. "
        "`preview_url` может быть `null` — картинка ещё не загружена, показывайте свою "
        "заглушку. Выбранный стиль передаётся в `POST /projects` полем `style_id` и "
        "сочетается с `template_id` и свободным `prompt` в любой комбинации. Требуется "
        "заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def list_styles(user: CurrentUser) -> StyleListResponse:
    """Каталог стилей. Одинаков для всех пользователей инстанса."""
    settings = get_settings()
    return StyleListResponse(
        items=[
            StyleOut(
                id=style.id,
                title=style.title,
                preview_url=style_service.preview_url(style.id, settings),
            )
            for style in style_service.list_styles()
        ]
    )


@router.get(
    "/{style_id}/preview",
    summary="Превью стиля",
    description=(
        "Отдаёт превью-картинку стиля (`image/jpeg`). Неизвестный стиль или отсутствующая "
        "картинка → `404`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    response_class=Response,
    responses={
        200: {
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
            "description": "Превью-картинка стиля.",
        },
        **problem_responses(401, 404, 429),
    },
)
async def get_style_preview(style_id: str, user: CurrentUser) -> Response:
    """Превью стиля. Нет стиля/картинки → 404."""
    path = style_service.preview_path(style_id)
    if path is None:
        raise not_found("Style preview not found.")
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        # Каталог меняется только с релизом образа — картинку можно держать в кэше.
        headers={"Cache-Control": "public, max-age=86400"},
    )
