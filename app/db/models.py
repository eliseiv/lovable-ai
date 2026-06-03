"""ORM-модели Postgres для happy-path Sprint 1 (docs/03-data-model.md).

Биллинговые таблицы (subscriptions, plan_quotas, usage_counters, billing_events) —
НЕ S1, здесь отсутствуют. ID — префиксные opaque строки; деньги — numeric(10,4) USD;
таймстампы — timestamptz (UTC).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import JobState


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Sprint 3 (ADR-007): стабильный sub из Apple identity token — identity-якорь upsert'а.
    # NULL допустим только для legacy S1 seeded-юзера. UNIQUE.
    apple_sub: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    # Legacy S1 (ADR-008 «Миграционный путь»): argon2id-хэш единственного seeded ключа.
    # С Sprint 3 реальные токены живут в api_tokens; поле становится nullable (fallback-путь).
    api_key_hash: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    adapty_customer_user_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("50.0000")
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    projects: Mapped[list[Project]] = relationship(back_populates="user")
    api_tokens: Mapped[list[ApiToken]] = relationship(back_populates="user")


class ApiToken(Base):
    """Opaque Bearer-токен `lv_<key_id>_<secret>` (Sprint 3, ADR-008, закрывает TD-004).

    Мульти-устройство: N активных строк на user_id. Индексируемый O(1) lookup по UNIQUE
    key_id → одна строка → один constant-time argon2-verify секрета. В БД хранится только
    публичный key_id + argon2id-хэш секрета; сам секрет не восстановим.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # Публичный индексируемый префикс ([a-z0-9]{16}, не секрет) — ЕДИНСТВЕННАЯ точка O(1)
    # lookup: WHERE key_id = :key_id AND revoked_at IS NULL. UNIQUE-индекс (ADR-008).
    key_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # argon2id-хэш СЕКРЕТНОЙ части ключа. Один constant-time verify после lookup по key_id.
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    device_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # best-effort апдейт при успешной аутентификации (UI/аудит, вне горячей транзакции).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = активен. Мягкий revoke (DELETE /v1/auth/tokens/{id}/logout): выставляет now().
    # Lookup игнорирует revoked_at IS NOT NULL → отозванный ключ сразу даёт 401.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_tokens")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # FK на revisions объявляется на уровне миграции (use_alter) во избежание
    # циклической зависимости projects↔revisions при создании таблиц.
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("revisions.id", use_alter=True, name="fk_projects_current_revision"),
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Sprint 4 (ADR-011): soft-delete-маркер. NULL = активен. DELETE /projects/{pid}
    # ставит now() → проект исключается из всех GET-листингов/деталей (фильтр
    # deleted_at IS NULL) и из подсчёта max_projects (projects_used); затем Celery
    # project.gc делает полный GC ресурсов и hard-delete строки.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="projects")
    jobs: Mapped[list[GenerationJob]] = relationship(
        back_populates="project", foreign_keys="GenerationJob.project_id"
    )

    __table_args__ = (
        # Частичный индекс для горячего фильтра активных проектов (deleted_at IS NULL):
        # листинги/детали/quota-gate max_projects (ADR-011).
        Index(
            "ix_projects_user_active",
            "user_id",
            postgresql_where=sql_text("deleted_at IS NULL"),
        ),
    )


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # Денормализация владельца (= projects.user_id): нужна для unique-constraint
    # идемпотентности и tenant-фильтрации без join (docs/03-data-model.md).
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state"),
        nullable=False,
        default=JobState.CREATED,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False, default="generation")
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_fix_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("5.0000")
    )
    spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0.0000")
    )
    wall_clock_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    # No-progress vs crash-resume дискриминатор (docs §C(d), ADR-005): True, когда
    # произведён НОВЫЙ failure-event (enter_fixing / невалидный патч Agent 4), ещё не
    # «потреблённый» гардом no-progress. Гард сбрасывает в False при проверке. Так
    # повтор той же сигнатуры на новом событии = no_progress, а переобработка того же
    # события после краша воркера (reconciler ре-диспетчеризует task_fix) — resume, не
    # no_progress. Внутреннее состояние гарда (как last_failure_signature), не контракт.
    failure_event_pending: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=sql_text("false")
    )
    # Финальная спека Agent 2: текст ≤ 16 KB inline, иначе spec_ref в S3.
    spec_tz: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_log_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project: Mapped[Project] = relationship(back_populates="jobs", foreign_keys=[project_id])
    events: Mapped[list[JobEvent]] = relationship(back_populates="job")
    questions: Mapped[list[Question]] = relationship(back_populates="job")
    answers: Mapped[list[Answer]] = relationship(back_populates="job")

    __table_args__ = (
        # Партиальный UNIQUE для дедупа POST /projects (опирается на денорм. user_id).
        Index(
            "uq_generation_jobs_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sql_text("idempotency_key IS NOT NULL"),
        ),
    )


class JobEvent(Base):
    """Аудит + источник для SSE. Append-only (docs/03-data-model.md → job_events)."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[GenerationJob] = relationship(back_populates="events")

    __table_args__ = (Index("ix_job_events_job_id_id", "job_id", "id"),)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    options: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    job: Mapped[GenerationJob] = relationship(back_populates="questions")
    answers: Mapped[list[Answer]] = relationship(back_populates="question")


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id"), nullable=False, index=True)
    # Денормализация для резюма (docs/03-data-model.md → answers.job_id).
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    question: Mapped[Question] = relationship(back_populates="answers")
    job: Mapped[GenerationJob] = relationship(back_populates="answers")


class Revision(Base):
    __tablename__ = "revisions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_artifact_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_from_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id"), nullable=False
    )
    is_good: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("project_id", "revision_no", name="uq_revisions_project_no"),
    )


class SiteDeployment(Base):
    __tablename__ = "site_deployments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), nullable=False)
    subdomain: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    live_url: Mapped[str] = mapped_column(Text, nullable=False)
    dist_artifact_ref: Mapped[str] = mapped_column(Text, nullable=False)
    build_log_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Subscription(Base):
    """Локальный кэш Adapty-подписки (docs/03-data-model.md → subscriptions).

    Источник истины — Adapty (ADR-004/009). Нет строки ⇒ трактуется как free.
    Поддерживается вебхуками (основной канал) + getProfile-ресинком (fallback).
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Одна актуальная строка-кэш на пользователя (upsert по user_id при ресинке/вебхуке).
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    # Маппится на plan_quotas.access_level (free/pro). Нет строки ⇒ free.
    access_level: Mapped[str] = mapped_column(String, nullable=False, default="free")
    product_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # active / expired / grace / billing_issue. На гейте проходят только active + grace.
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    store: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Дедлайн grace (expire/refund + GRACE_PERIOD_DAYS). subscription_sweep сносит сайты
    # при status='grace' AND grace_until < now(). Renew/started в grace → NULL.
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    will_renew: Mapped[bool] = mapped_column(nullable=False, default=False)
    adapty_transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Сырой профиль/событие Adapty.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Последняя ресинхронизация. TTL свежести — BILLING_RESYNC_INTERVAL_S; протух ⇒
    # lazy-ресинк на гейте/billing/me. Также используется как «таймстамп вебхук-состояния»
    # для приоритета вебхука над ресинком (resync не перетирает более свежее состояние).
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlanQuota(Base):
    """Лимиты тарифа (docs/03-data-model.md → plan_quotas). Сидится Alembic-миграцией."""

    __tablename__ = "plan_quotas"

    access_level: Mapped[str] = mapped_column(String, primary_key=True)
    monthly_generations: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL = безлимит конкурентности (в сидинге не используется; free=1/pro=3).
    max_concurrent_jobs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL = безлимит проектов (Pro).
    max_projects: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("5.0000")
    )
    # Sprint 5 (ADR-014): бизнес-квота правок (kind='edit')/мес — ОТДЕЛЬНАЯ от
    # monthly_generations. NULL = безлимит (Pro). Энфорс — quota-gate на /edits против
    # edit_usage_counters.edits_used. Сидинг Free=5, Pro=NULL (миграция 0006).
    monthly_edits: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UsageCounter(Base):
    """Месячный счётчик генераций (docs/03-data-model.md → usage_counters).

    Инкремент на УСПЕШНЫЙ старт генерации (kind='generation'), не на POST /projects /
    /answers. Атомарный upsert ON CONFLICT (user_id, period); идемпотентно по job_id.
    """

    __tablename__ = "usage_counters"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String, primary_key=True)  # YYYY-MM (UTC)
    generations_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EditUsageCounter(Base):
    """Месячный счётчик правок (kind='edit', Sprint 5, ADR-014, docs §edit_usage_counters).

    Лимит правок независим от квоты генераций — отдельная таблица (НЕ usage_counters).
    Инкремент на УСПЕШНОМ старте edit-джобы (постановка первой task_fix-edit), не на
    POST /edits и не на rollback. Атомарный upsert ON CONFLICT (user_id, period);
    идемпотентно по job_id (job_events-маркер edit_usage_counted).
    """

    __tablename__ = "edit_usage_counters"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String, primary_key=True)  # YYYY-MM (UTC)
    edits_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DeviceToken(Base):
    """APNs device token для push-нотификаций (Sprint 5, ADR-013, docs §device_tokens).

    Мульти-устройство: N токенов на user. Upsert по UNIQUE (user_id, apns_token) при
    POST /v1/devices (повтор сбрасывает invalidated_at). Выборка для push игнорирует
    invalidated_at IS NOT NULL (мёртвые/отписанные токены). Cross-tenant: выборка строго
    по user_id владельца джобы.
    """

    __tablename__ = "device_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # dev_...
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    apns_token: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False, default="ios")
    # sandbox / production — определяет APNs-хост (override-дефолт APNS_ENV).
    environment: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Последняя успешная доставка (аудит, best-effort).
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = активен. now() при APNs 410/400 BadDeviceToken или явной отписке.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "apns_token", name="uq_device_tokens_user_token"),
    )


class BillingEvent(Base):
    """Adapty webhook ledger (docs/03-data-model.md → billing_events).

    adapty_event_id UNIQUE — единственная точка идемпотентности обработки вебхуков
    (повтор → 200 no-op). user_id NULL допустим (рассинхрон customer_user_id).
    """

    __tablename__ = "billing_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # = webhook.event_id. UNIQUE: атомарный дедуп на уровне БД (защита от replay).
    adapty_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    # NULL до маппинга customer_user_id → user (рассинхрон: событие не теряем).
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # NULL = принят, не обработан (добивается ресинком/повтором доставки).
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LlmUsage(Base):
    """Cost-ledger. Запись на каждый вызол Claude (docs/03-data-model.md → llm_usage)."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0.0000")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
