"""Схемы REST API (docs/modules/api/02-api-contracts.md).

Контракт iOS-клиента. Все async-операции возвращают 202 + job_id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Problem(BaseModel):
    """Стандартная модель ошибки RFC 7807 (`application/problem+json`).

    Возвращается на ошибочных ответах (`401`/`402`/`404`/`409`/`422`/`429`). Доменные поля
    (`reason`, `required_entitlement`, `retry_after`, `current_state`) присутствуют там, где
    применимо к конкретной ошибке.
    """

    type: str = Field(description="URI-идентификатор типа ошибки.")
    title: str = Field(description="Краткое название ошибки.")
    status: int = Field(description="HTTP-код ответа.")
    detail: str = Field(description="Человекочитаемое описание причины ошибки.")
    reason: str | None = Field(
        default=None, description="Доменный код причины (например, для `402`/`429`)."
    )
    required_entitlement: str | None = Field(
        default=None, description="Минимальный тариф, снимающий ограничение (для `402`)."
    )
    retry_after: int | None = Field(
        default=None, description="Через сколько секунд повторить запрос (для `429`)."
    )
    current_state: str | None = Field(
        default=None, description="Текущий статус задачи на момент конфликта (для `409`)."
    )


# --- POST /projects ---


class CreateProjectRequest(BaseModel):
    prompt: str = Field(min_length=1, description="Текстовое описание желаемого сайта.")
    title: str | None = Field(default=None, description="Необязательное название проекта.")


class CreateProjectResponse(BaseModel):
    project_id: str = Field(description="Идентификатор созданного проекта.")
    job_id: str = Field(description="Идентификатор задачи генерации (для отслеживания статуса).")


# --- GET /projects · GET /projects/{pid} ---


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Идентификатор проекта.")
    title: str | None = Field(description="Название проекта.")
    prompt: str = Field(description="Исходное текстовое описание сайта.")
    current_revision_id: str | None = Field(description="Идентификатор текущей ревизии.")
    live_url: str | None = Field(
        default=None, description="Адрес работающего сайта (если опубликован)."
    )
    created_at: datetime = Field(description="Дата и время создания проекта.")


class ProjectListResponse(BaseModel):
    projects: list[ProjectOut] = Field(description="Список проектов пользователя.")


# --- DELETE /projects/{pid} ---


class DeleteProjectResponse(BaseModel):
    project_id: str = Field(description="Идентификатор удаляемого проекта.")
    status: str = Field(description="Статус удаления (`deleting` — очистка запущена).")


# --- GET /jobs/{jid} ---


class JobStatusResponse(BaseModel):
    id: str = Field(description="Идентификатор задачи.")
    project_id: str = Field(description="Идентификатор проекта задачи.")
    state: str = Field(
        description="Текущий этап задачи (`CREATED`, `INTERVIEWING`, "
        "`AWAITING_CLARIFICATION`, `SPECCING`, `BUILDING`, `DEPLOYING`, `LIVE`, `FIXING`, "
        "`FAILED`)."
    )
    retry_count: int = Field(description="Число выполненных попыток исправления.")
    failure_reason: str | None = Field(
        default=None, description="Машинный код причины при неудаче (`FAILED`)."
    )
    live_url: str | None = Field(
        default=None, description="Адрес работающего сайта (по завершении, `LIVE`)."
    )
    updated_at: datetime = Field(description="Время последнего обновления статуса.")


# --- GET /jobs/{jid}/questions ---


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Идентификатор вопроса.")
    position: int = Field(description="Порядковый номер вопроса.")
    text: str = Field(description="Текст вопроса.")
    kind: str | None = Field(
        default=None, description="Тип вопроса: `choice` (выбор) или `free_text` (свободный)."
    )
    options: list[Any] | None = Field(
        default=None, description="Варианты ответа для вопроса типа `choice`."
    )


class QuestionsResponse(BaseModel):
    questions: list[QuestionOut] = Field(description="Список уточняющих вопросов.")


# --- POST /jobs/{jid}/answers ---


class AnswerItem(BaseModel):
    question_id: str = Field(description="Идентификатор вопроса, на который дан ответ.")
    text: str = Field(description="Текст ответа пользователя.")


class SubmitAnswersRequest(BaseModel):
    answers: list[AnswerItem] = Field(min_length=1, description="Ответы на уточняющие вопросы.")


class SubmitAnswersResponse(BaseModel):
    job_id: str = Field(description="Идентификатор задачи, генерация которой продолжена.")


# --- POST /auth/apple ---


class AppleSignInRequest(BaseModel):
    identity_token: str = Field(
        min_length=1, description="Identity-токен Apple (Sign in with Apple)."
    )
    nonce: str | None = Field(
        default=None, description="Одноразовое значение nonce (если использовалось)."
    )
    device_label: str | None = Field(default=None, description="Необязательная метка устройства.")


class AppleSignInResponse(BaseModel):
    api_key: str = Field(description="Bearer-ключ для запросов (`lv_<key_id>_<secret>`).")
    token_id: str = Field(description="Идентификатор выданного токена устройства.")
    user_id: str = Field(description="Идентификатор пользователя.")


# --- GET /auth/tokens ---


class TokenOut(BaseModel):
    id: str = Field(description="Идентификатор токена.")
    key_id: str = Field(description="Публичный идентификатор ключа.")
    device_label: str | None = Field(default=None, description="Метка устройства.")
    created_at: datetime = Field(description="Дата и время выдачи токена.")
    last_used_at: datetime | None = Field(
        default=None, description="Время последнего использования токена."
    )
    current: bool = Field(description="Является ли токеном текущего запроса.")


class TokensListResponse(BaseModel):
    tokens: list[TokenOut] = Field(description="Список активных токенов (устройств).")


# --- POST /projects/{pid}/edits ---


class CreateEditRequest(BaseModel):
    instruction: str = Field(min_length=1, description="Текстовая инструкция к правке сайта.")


class CreateEditResponse(BaseModel):
    job_id: str = Field(description="Идентификатор задачи правки (для отслеживания статуса).")


# --- GET /projects/{pid}/revisions ---


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Идентификатор ревизии.")
    revision_no: int = Field(description="Порядковый номер ревизии.")
    is_good: bool = Field(description="Была ли ревизия успешно опубликована.")
    created_from_job_id: str = Field(description="Идентификатор задачи, создавшей ревизию.")
    created_at: datetime = Field(description="Дата и время создания ревизии.")


class RevisionsListResponse(BaseModel):
    current_revision_id: str | None = Field(description="Идентификатор текущей активной ревизии.")
    revisions: list[RevisionOut] = Field(description="История ревизий проекта.")


# --- POST /projects/{pid}/revisions/{revision_no}/rollback ---


class RollbackResponse(BaseModel):
    job_id: str = Field(description="Идентификатор задачи отката (для отслеживания статуса).")
    target_revision_no: int = Field(description="Номер ревизии, на которую выполняется откат.")


# --- POST /devices · DELETE /devices/{apns_token} ---


class RegisterDeviceRequest(BaseModel):
    apns_token: str = Field(min_length=1, description="Токен устройства для push-уведомлений.")
    platform: str = Field(default="ios", description="Платформа устройства (`ios`).")
    environment: str = Field(description="Окружение push: `sandbox` или `production`.")


class RegisterDeviceResponse(BaseModel):
    id: str = Field(description="Идентификатор зарегистрированного устройства.")


# --- GET /v1/billing/me ---


class BillingQuota(BaseModel):
    monthly_generations: int = Field(description="Лимит генераций в месяц.")
    generations_used: int = Field(description="Использовано генераций в текущем месяце.")
    generations_remaining: int = Field(description="Остаток генераций в текущем месяце.")
    monthly_edits: int | None = Field(description="Лимит правок в месяц (`null` — безлимит).")
    edits_used: int = Field(description="Использовано правок в текущем месяце.")
    edits_remaining: int | None = Field(
        description="Остаток правок в текущем месяце (`null` — безлимит)."
    )
    max_concurrent_jobs: int | None = Field(
        description="Лимит одновременных задач (`null` — безлимит)."
    )
    active_jobs: int = Field(description="Число активных задач сейчас.")
    max_projects: int | None = Field(description="Лимит проектов (`null` — безлимит).")
    projects_used: int = Field(description="Число активных проектов.")


class BillingMeResponse(BaseModel):
    access_level: str = Field(description="Текущий тариф (`free` / `pro`).")
    status: str = Field(description="Статус подписки.")
    period: str = Field(description="Расчётный период в формате `YYYY-MM`.")
    quota: BillingQuota = Field(description="Лимиты и остатки квот.")


# --- Health/readiness (служебное, не в публичной схеме) ---


class HealthResponse(BaseModel):
    status: str
