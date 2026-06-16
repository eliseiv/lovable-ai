"""Генерация префиксных opaque ID (docs/03-data-model.md).

`u_`, `p_`, `j_`, `r_`, `d_` для сущностей; `q_`, `a_` для вопросов/ответов.
Subdomain — отдельный opaque [a-z0-9]{16} без префикса (docs/modules/deploy/03-architecture.md).
"""

from __future__ import annotations

import secrets
import string

_ALPHABET = string.ascii_lowercase + string.digits
_SUBDOMAIN_ALPHABET = string.ascii_lowercase + string.digits


def _opaque(prefix: str, n: int = 24) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(n))
    return f"{prefix}{body}"


def new_user_id() -> str:
    return _opaque("u_")


def new_token_id() -> str:
    """Opaque ID строки api_tokens (`t_...`), адресует токен в DELETE /v1/auth/tokens/{id}."""
    return _opaque("t_")


def new_key_id() -> str:
    """Публичный индексируемый префикс ключа `[a-z0-9]{16}` (НЕ секрет, ADR-008)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(16))


def new_token_secret() -> str:
    """Высокоэнтропийная секретная часть ключа (≥32 байта энтропии, ADR-008).

    `token_urlsafe(32)` → 256 бит энтропии. В БД хранится только argon2id-хэш.
    Алфавит токена urlsafe-base64 не содержит `_` коллизий с разделителем формата,
    т.к. парсинг ключа `lv_<key_id>_<secret>` режется строго по первым двум `_`.
    """
    return secrets.token_urlsafe(32)


def new_project_id() -> str:
    return _opaque("p_")


def new_job_id() -> str:
    return _opaque("j_")


def new_revision_id() -> str:
    return _opaque("r_")


def new_deployment_id() -> str:
    return _opaque("d_")


def new_question_id() -> str:
    return _opaque("q_")


def new_answer_id() -> str:
    return _opaque("a_")


def new_subscription_id() -> str:
    """Opaque ID строки subscriptions (`s_...`)."""
    return _opaque("s_")


def new_device_token_id() -> str:
    """Opaque ID строки device_tokens (`dev_...`, Sprint 5 ADR-013)."""
    return _opaque("dev_")


def new_credit_grant_id() -> str:
    """Opaque ID строки credit_grants (`cg_...`, ADR-021 бонус-генерации)."""
    return _opaque("cg_")


def new_attachment_id() -> str:
    """Opaque ID строки attachments (`att_...`, ADR-034 user image attachments).

    Часть детерминированного пути инжекта/ключа S3 (`uploads/{project_id}/{att_id}.{ext}`,
    docs/03-data-model.md → attachments).
    """
    return _opaque("att_")


def new_subdomain() -> str:
    """Opaque-идентификатор деплоя [a-z0-9]{16} (НЕ project_id)."""
    return "".join(secrets.choice(_SUBDOMAIN_ALPHABET) for _ in range(16))
