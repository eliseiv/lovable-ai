"""Declarative base + общие миксины для SQLAlchemy 2.0."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


def utc_now_column() -> Mapped[datetime]:
    """Колонка timestamptz со server-side default now() (UTC)."""
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
