"""Contract: response-схемы endpoints + RFC-7807 ошибки (docs/modules/api/02-api-contracts.md)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id, new_question_id
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Question

pytestmark = pytest.mark.asyncio


async def _job_with_questions(session, user_id, state=JobState.AWAITING_CLARIFICATION):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title="T"))
    session.add(
        GenerationJob(
            id=jid,
            project_id=pid,
            user_id=user_id,
            state=state,
            kind="generation",
            budget_usd=Decimal("5.0000"),
        )
    )
    await session.flush()
    qids = []
    for i in range(2):
        qid = new_question_id()
        qids.append(qid)
        session.add(
            Question(
                id=qid, job_id=jid, position=i + 1, text=f"Q{i}", kind="free_text", options=None
            )
        )
    await session.flush()
    return pid, jid, qids


async def test_create_project_response_schema(client, auth_headers, seeded_user, no_side_effects):
    resp = await client.post(
        "/v1/projects",
        json={"prompt": "build me a site"},
        headers={**auth_headers, "Idempotency-Key": "ck"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert set(body.keys()) == {"project_id", "job_id"}


async def test_project_validation_empty_prompt_422(client, auth_headers, seeded_user):
    resp = await client.post(
        "/v1/projects",
        json={"prompt": ""},
        headers={**auth_headers, "Idempotency-Key": "ck2"},
    )
    # Pydantic min_length=1 → FastAPI 422.
    assert resp.status_code == 422


async def test_job_status_response_schema(client, auth_headers, session, seeded_user):
    _, jid, _ = await _job_with_questions(session, seeded_user.id, JobState.CREATED)
    resp = await client.get(f"/v1/jobs/{jid}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("id", "project_id", "state", "retry_count", "updated_at"):
        assert key in body
    assert body["state"] == "CREATED"
    assert body["live_url"] is None


async def test_questions_response_schema_ordered(client, auth_headers, session, seeded_user):
    _, jid, qids = await _job_with_questions(session, seeded_user.id)
    resp = await client.get(f"/v1/jobs/{jid}/questions", headers=auth_headers)
    assert resp.status_code == 200
    questions = resp.json()["questions"]
    assert len(questions) == 2
    positions = [q["position"] for q in questions]
    assert positions == sorted(positions)
    for q in questions:
        assert set(q.keys()) >= {"id", "position", "text"}


async def test_answers_202_and_200_status_codes(
    client, auth_headers, session, seeded_user, no_side_effects
):
    _, jid, qids = await _job_with_questions(session, seeded_user.id)
    payload = {
        "answers": [{"question_id": qids[0], "text": "a"}, {"question_id": qids[1], "text": "b"}]
    }
    r1 = await client.post(f"/v1/jobs/{jid}/answers", json=payload, headers=auth_headers)
    assert r1.status_code == 202
    assert r1.json()["job_id"] == jid
    r2 = await client.post(f"/v1/jobs/{jid}/answers", json=payload, headers=auth_headers)
    assert r2.status_code == 200


async def test_answers_409_conflict_carries_current_state(
    client, auth_headers, session, seeded_user, no_side_effects
):
    _, jid, qids = await _job_with_questions(session, seeded_user.id)
    payload = {
        "answers": [{"question_id": qids[0], "text": "a"}, {"question_id": qids[1], "text": "b"}]
    }
    await client.post(f"/v1/jobs/{jid}/answers", json=payload, headers=auth_headers)
    other = {
        "answers": [{"question_id": qids[0], "text": "X"}, {"question_id": qids[1], "text": "Y"}]
    }
    resp = await client.post(f"/v1/jobs/{jid}/answers", json=other, headers=auth_headers)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == 409
    assert body["type"].endswith("/conflict")
    assert body.get("current_state") == "SPECCING"


async def test_answers_422_partial(client, auth_headers, session, seeded_user):
    _, jid, qids = await _job_with_questions(session, seeded_user.id)
    payload = {"answers": [{"question_id": qids[0], "text": "a"}]}
    resp = await client.post(f"/v1/jobs/{jid}/answers", json=payload, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["status"] == 422


async def test_problem_json_shape(client, auth_headers, session, seeded_user):
    resp = await client.get("/v1/jobs/j_nonexistent00000000000", headers=auth_headers)
    assert resp.status_code == 404
    body = resp.json()
    for key in ("type", "title", "status", "detail"):
        assert key in body
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_healthz_no_auth(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
