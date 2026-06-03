"""Initial Sprint 1 schema (happy-path tables, docs/03-data-model.md).

Создаёт users, projects, generation_jobs, job_events, questions, answers,
revisions, site_deployments, llm_usage + enum job_state. Биллинговые таблицы — НЕ S1.

Revision ID: 20260602_0001
Revises:
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260602_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_STATES = (
    "CREATED",
    "INTERVIEWING",
    "AWAITING_CLARIFICATION",
    "SPECCING",
    "BUILDING",
    "DEPLOYING",
    "LIVE",
    "FIXING",
    "FAILED",
)


def upgrade() -> None:
    job_state = postgresql.ENUM(*_JOB_STATES, name="job_state")
    job_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("api_key_hash", sa.Text(), nullable=False),
        sa.Column("adapty_customer_user_id", sa.Text(), nullable=True),
        sa.Column("monthly_budget_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("api_key_hash", name="uq_users_api_key_hash"),
        sa.UniqueConstraint("adapty_customer_user_id", name="uq_users_adapty_customer"),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("current_revision_id", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_projects_user"),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])

    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(*_JOB_STATES, name="job_state", create_type=False),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_fix_attempts", sa.Integer(), nullable=False),
        sa.Column("budget_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("spend_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("wall_clock_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_signature", sa.Text(), nullable=True),
        sa.Column("spec_tz", sa.Text(), nullable=True),
        sa.Column("spec_ref", sa.Text(), nullable=True),
        sa.Column("failure_log_ref", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_jobs_project"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_jobs_user"),
    )
    op.create_index("ix_generation_jobs_project_id", "generation_jobs", ["project_id"])
    op.create_index("ix_generation_jobs_user_id", "generation_jobs", ["user_id"])
    op.create_index("ix_generation_jobs_state", "generation_jobs", ["state"])
    # Партиальный UNIQUE для идемпотентности POST /projects.
    op.create_index(
        "uq_generation_jobs_idempotency",
        "generation_jobs",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "revisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source_artifact_ref", sa.Text(), nullable=False),
        sa.Column("created_from_job_id", sa.String(), nullable=False),
        sa.Column("is_good", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_revisions_project"),
        sa.ForeignKeyConstraint(
            ["created_from_job_id"],
            ["generation_jobs.id"],
            name="fk_revisions_job",
        ),
        sa.UniqueConstraint("project_id", "revision_no", name="uq_revisions_project_no"),
    )
    op.create_index("ix_revisions_project_id", "revisions", ["project_id"])

    # FK projects.current_revision_id → revisions (use_alter: цикл projects↔revisions).
    op.create_foreign_key(
        "fk_projects_current_revision",
        "projects",
        "revisions",
        ["current_revision_id"],
        ["id"],
    )

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("from_state", sa.String(), nullable=True),
        sa.Column("to_state", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["generation_jobs.id"], name="fk_job_events_job"),
    )
    op.create_index("ix_job_events_job_id_id", "job_events", ["job_id", "id"])

    op.create_table(
        "questions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["generation_jobs.id"], name="fk_questions_job"),
    )
    op.create_index("ix_questions_job_id", "questions", ["job_id"])

    op.create_table(
        "answers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("question_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], name="fk_answers_question"),
        sa.ForeignKeyConstraint(["job_id"], ["generation_jobs.id"], name="fk_answers_job"),
    )
    op.create_index("ix_answers_question_id", "answers", ["question_id"])
    op.create_index("ix_answers_job_id", "answers", ["job_id"])

    op.create_table(
        "site_deployments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("subdomain", sa.String(), nullable=False),
        sa.Column("live_url", sa.Text(), nullable=False),
        sa.Column("dist_artifact_ref", sa.Text(), nullable=False),
        sa.Column("build_log_ref", sa.Text(), nullable=True),
        sa.Column("container_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_deployments_project"),
        sa.ForeignKeyConstraint(["revision_id"], ["revisions.id"], name="fk_deployments_revision"),
        sa.UniqueConstraint("subdomain", name="uq_site_deployments_subdomain"),
    )
    op.create_index("ix_site_deployments_project_id", "site_deployments", ["project_id"])

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["generation_jobs.id"], name="fk_llm_usage_job"),
    )
    op.create_index("ix_llm_usage_job_id", "llm_usage", ["job_id"])


def downgrade() -> None:
    op.drop_table("llm_usage")
    op.drop_table("site_deployments")
    op.drop_table("answers")
    op.drop_table("questions")
    op.drop_table("job_events")
    op.drop_constraint("fk_projects_current_revision", "projects", type_="foreignkey")
    op.drop_table("revisions")
    op.drop_table("generation_jobs")
    op.drop_table("projects")
    op.drop_table("users")
    postgresql.ENUM(name="job_state").drop(op.get_bind(), checkfirst=True)
