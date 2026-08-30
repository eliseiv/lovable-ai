"""Integration: `GET /v1/admin/costs/daily` — расширение контракта broad-crm v1.3.

Покрытие (контракт заморожен на стороне CRM: путь, имена query и полей ответа):
  - агрегация день × провайдер из `llm_usage`, свёртка моделей одного провайдера;
  - сортировка `date ASC, provider ASC` и стабильная пагинация `limit/offset` + `total`;
  - границы периода — календарные дни UTC включительно с обеих сторон;
  - отсутствие строки за (день, провайдер) = расхода не было (нули не досыпаются);
  - `400` на `date_from > date_to` и на период длиннее 92 дней;
  - `403` без `X-Admin-Key` (общий гейт админ-плоскости).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, LlmUsage, Project, User

pytestmark = pytest.mark.asyncio

_UID = "u_costs_daily0001"


async def _user_with_job(session) -> str:  # noqa: ANN001
    session.add(
        User(
            id=_UID,
            api_key_hash=hash_api_key(f"{_UID}-legacy-key"),
            monthly_budget_usd=Decimal("50.0000"),
            status="active",
        )
    )
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=_UID, prompt="build me a site", title=None))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=_UID,
            state=JobState.CREATED,
            kind="generation",
            budget_usd=Decimal("5.0000"),
            spend_usd=Decimal("0.0000"),
        )
    )
    await session.flush()
    return jid


async def _usage(session, job_id: str, *, model: str, cost: str, when: datetime) -> None:  # noqa: ANN001
    row = LlmUsage(
        job_id=job_id,
        agent="agent1",
        model=model,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=1,
        cache_write_tokens=0,
        cost_usd=Decimal(cost),
    )
    session.add(row)
    await session.flush()
    row.created_at = when
    await session.flush()


async def test_aggregates_by_day_and_provider(client, session, admin_headers):
    job_id = await _user_with_job(session)
    day1 = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    day2 = datetime(2026, 8, 11, 23, 59, tzinfo=UTC)
    # Два вызова разных моделей одного провайдера за один день → одна строка с суммой.
    await _usage(session, job_id, model="claude-sonnet-4-6", cost="0.0100", when=day1)
    await _usage(session, job_id, model="claude-opus-4-8", cost="0.0200", when=day1)
    await _usage(session, job_id, model="gpt-5.5", cost="0.0300", when=day1)
    await _usage(session, job_id, model="gpt-5.4-mini", cost="0.0400", when=day2)

    resp = await client.get(
        "/v1/admin/costs/daily",
        params={"date_from": "2026-08-10", "date_to": "2026-08-11"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    rows = {(item["date"], item["provider"]): item for item in body["items"]}

    assert body["total"] == 3
    assert rows[("2026-08-10", "anthropic")]["spend_usd"] == pytest.approx(0.03)
    assert rows[("2026-08-10", "anthropic")]["requests"] == 2
    assert rows[("2026-08-10", "anthropic")]["tokens"] == pytest.approx(32.0)
    assert rows[("2026-08-10", "openai")]["spend_usd"] == pytest.approx(0.03)
    assert rows[("2026-08-11", "openai")]["requests"] == 1
    # За (2026-08-11, anthropic) расхода не было → строки нет, ноль не досыпается.
    assert ("2026-08-11", "anthropic") not in rows


async def test_sorted_by_date_then_provider_and_paginated(client, session, admin_headers):
    job_id = await _user_with_job(session)
    await _usage(
        session, job_id, model="gpt-5.5", cost="0.0100", when=datetime(2026, 8, 10, 1, tzinfo=UTC)
    )
    await _usage(
        session,
        job_id,
        model="claude-opus-4-8",
        cost="0.0200",
        when=datetime(2026, 8, 10, 2, tzinfo=UTC),
    )
    await _usage(
        session, job_id, model="gpt-5.5", cost="0.0300", when=datetime(2026, 8, 12, 3, tzinfo=UTC)
    )

    resp = await client.get(
        "/v1/admin/costs/daily",
        params={"date_from": "2026-08-10", "date_to": "2026-08-12"},
        headers=admin_headers,
    )
    body = resp.json()
    assert [(i["date"], i["provider"]) for i in body["items"]] == [
        ("2026-08-10", "anthropic"),
        ("2026-08-10", "openai"),
        ("2026-08-12", "openai"),
    ]

    page = await client.get(
        "/v1/admin/costs/daily",
        params={
            "date_from": "2026-08-10",
            "date_to": "2026-08-12",
            "limit": 1,
            "offset": 1,
        },
        headers=admin_headers,
    )
    page_body = page.json()
    assert page_body["total"] == 3
    assert [(i["date"], i["provider"]) for i in page_body["items"]] == [("2026-08-10", "openai")]


async def test_period_bounds_are_inclusive_utc_days(client, session, admin_headers):
    job_id = await _user_with_job(session)
    await _usage(
        session,
        job_id,
        model="gpt-5.5",
        cost="0.0100",
        when=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
    )
    await _usage(
        session,
        job_id,
        model="gpt-5.5",
        cost="0.0200",
        when=datetime(2026, 8, 12, 23, 59, 59, tzinfo=UTC),
    )
    await _usage(
        session,
        job_id,
        model="gpt-5.5",
        cost="0.0400",
        when=datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
    )

    resp = await client.get(
        "/v1/admin/costs/daily",
        params={"date_from": "2026-08-10", "date_to": "2026-08-12"},
        headers=admin_headers,
    )
    body = resp.json()
    assert [i["date"] for i in body["items"]] == ["2026-08-10", "2026-08-12"]


async def test_reversed_period_is_400(client, admin_headers):
    resp = await client.get(
        "/v1/admin/costs/daily",
        params={"date_from": "2026-08-12", "date_to": "2026-08-10"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


async def test_period_longer_than_92_days_is_400(client, admin_headers):
    start = datetime(2026, 5, 1, tzinfo=UTC).date()
    resp = await client.get(
        "/v1/admin/costs/daily",
        params={
            "date_from": start.isoformat(),
            "date_to": (start + timedelta(days=92)).isoformat(),
        },
        headers=admin_headers,
    )
    assert resp.status_code == 400


async def test_exactly_92_days_is_allowed(client, admin_headers):
    start = datetime(2026, 5, 1, tzinfo=UTC).date()
    resp = await client.get(
        "/v1/admin/costs/daily",
        params={
            "date_from": start.isoformat(),
            "date_to": (start + timedelta(days=91)).isoformat(),
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200


async def test_requires_admin_key(client):
    resp = await client.get(
        "/v1/admin/costs/daily",
        params={"date_from": "2026-08-10", "date_to": "2026-08-11"},
    )
    assert resp.status_code == 403


async def test_user_card_revenue_providers_split_by_model(client, session, admin_headers):
    """Карточка юзера: разбивка расхода по провайдерам из ledger, а не константа."""
    job_id = await _user_with_job(session)
    job = await session.get(GenerationJob, job_id)
    await _usage(
        session,
        job_id,
        model="claude-sonnet-4-6",
        cost="0.0100",
        when=datetime(2026, 8, 10, 1, tzinfo=UTC),
    )
    await _usage(
        session, job_id, model="gpt-5.5", cost="0.0300", when=datetime(2026, 8, 10, 2, tzinfo=UTC)
    )
    job.spend_usd = Decimal("0.0400")
    await session.flush()

    resp = await client.get(f"/v1/admin/users/{_UID}", headers=admin_headers)
    assert resp.status_code == 200
    revenue = resp.json()["revenue"]
    assert revenue["api_cost_usd"] == pytest.approx(0.04)
    assert revenue["providers"] == {
        "anthropic": pytest.approx(0.01),
        "openai": pytest.approx(0.03),
    }
