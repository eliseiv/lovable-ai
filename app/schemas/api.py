"""Схемы REST API (docs/modules/api/02-api-contracts.md).

Контракт iOS-клиента. Все async-операции возвращают 202 + job_id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- POST /projects ---


class CreateProjectRequest(BaseModel):
    prompt: str = Field(min_length=1)
    title: str | None = None


class CreateProjectResponse(BaseModel):
    project_id: str
    job_id: str


# --- GET /projects · GET /projects/{pid} ---


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    prompt: str
    current_revision_id: str | None
    live_url: str | None = None
    created_at: datetime


class ProjectListResponse(BaseModel):
    projects: list[ProjectOut]


# --- DELETE /projects/{pid} (Sprint 4, ADR-011) ---


class DeleteProjectResponse(BaseModel):
    project_id: str
    status: str  # "deleting" — async GC поставлен (project.gc)


# --- GET /jobs/{jid} ---


class JobStatusResponse(BaseModel):
    id: str
    project_id: str
    state: str
    retry_count: int
    failure_reason: str | None = None
    live_url: str | None = None
    updated_at: datetime


# --- GET /jobs/{jid}/questions ---


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    text: str
    kind: str | None = None
    options: list[Any] | None = None


class QuestionsResponse(BaseModel):
    questions: list[QuestionOut]


# --- POST /jobs/{jid}/answers ---


class AnswerItem(BaseModel):
    question_id: str
    text: str


class SubmitAnswersRequest(BaseModel):
    answers: list[AnswerItem] = Field(min_length=1)


class SubmitAnswersResponse(BaseModel):
    job_id: str


# --- POST /auth/apple ---


class AppleSignInRequest(BaseModel):
    identity_token: str = Field(min_length=1)
    nonce: str | None = None
    device_label: str | None = None


class AppleSignInResponse(BaseModel):
    api_key: str
    token_id: str
    user_id: str


# --- GET /auth/tokens ---


class TokenOut(BaseModel):
    id: str
    key_id: str
    device_label: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    current: bool


class TokensListResponse(BaseModel):
    tokens: list[TokenOut]


# --- POST /projects/{pid}/edits (Sprint 5, ADR-014) ---


class CreateEditRequest(BaseModel):
    instruction: str = Field(min_length=1)


class CreateEditResponse(BaseModel):
    job_id: str


# --- GET /projects/{pid}/revisions (Sprint 5, ADR-014) ---


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    revision_no: int
    is_good: bool
    created_from_job_id: str
    created_at: datetime


class RevisionsListResponse(BaseModel):
    current_revision_id: str | None
    revisions: list[RevisionOut]


# --- POST /projects/{pid}/revisions/{revision_no}/rollback (Sprint 5, ADR-014) ---


class RollbackResponse(BaseModel):
    job_id: str
    target_revision_no: int


# --- POST /devices · DELETE /devices/{apns_token} (Sprint 5, ADR-013) ---


class RegisterDeviceRequest(BaseModel):
    apns_token: str = Field(min_length=1)
    platform: str = "ios"
    environment: str  # sandbox | production


class RegisterDeviceResponse(BaseModel):
    id: str


# --- GET /v1/billing/me (docs/modules/billing/02-api-contracts.md §2) ---


class BillingQuota(BaseModel):
    monthly_generations: int
    generations_used: int
    generations_remaining: int
    # Sprint 5 (ADR-014): отдельный лимит правок. NULL = безлимит (Pro).
    monthly_edits: int | None
    edits_used: int
    edits_remaining: int | None
    max_concurrent_jobs: int | None
    active_jobs: int
    max_projects: int | None
    projects_used: int


class BillingMeResponse(BaseModel):
    access_level: str
    status: str
    period: str
    quota: BillingQuota


# --- Health/readiness ---


class HealthResponse(BaseModel):
    status: str
