"""Router /models — каталог моделей генерации (ADR-051).

Третья ось выбора рядом с шаблоном и стилем: «чем строим». Выбранный пресет применяется ко
ВСЕМ агентам пайплайна и передаётся в `POST /projects` полем `model_id` — отдельного входа в
генерацию, как и у шаблонов со стилями, здесь нет.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import CurrentUser
from app.api.errors import problem_responses
from app.core.config import get_settings
from app.schemas.api import GenerationModelListResponse, GenerationModelOut
from app.services import model_service

router = APIRouter(prefix="/models", tags=["Проекты"])


@router.get(
    "",
    response_model=GenerationModelListResponse,
    summary="Каталог моделей генерации",
    description=(
        "Возвращает модели, которыми можно построить сайт: идентификатор, название и краткое "
        "описание отличий. Выбранная модель применяется ко всем шагам генерации сразу и "
        "передаётся в `POST /projects` полем `model_id`; без выбора используются значения "
        "инстанса по умолчанию. Набор зависит от провайдера инстанса, поэтому идентификаторы "
        "берите только из этого ответа. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def list_models(user: CurrentUser) -> GenerationModelListResponse:
    """Каталог моделей. Одинаков для всех пользователей инстанса."""
    settings = get_settings()
    return GenerationModelListResponse(
        items=[
            GenerationModelOut(id=m.id, title=m.title, description=m.description)
            for m in model_service.list_models(settings)
        ]
    )
