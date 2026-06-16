"""РЕАЛЬНЫЙ E2E на поднятом dev-стеке (docs/06-testing-strategy.md → E2E happy-path).

Требует ОКРУЖЕНИЯ: docker compose dev-стек поднят, реальный ANTHROPIC_API_KEY,
Docker + WSL2. Без окружения тест SKIP (не выдумывает результат).

Запуск окружения (точная инструкция):
    # 1. .env с реальными секретами (ANTHROPIC_API_KEY, SEED_API_KEY, POSTGRES_*, MINIO_*,
    #    APPS_DOMAIN=apps.localhost, TRAEFIK_NETWORK=lovable_traefik, NGINX_IMAGE=nginx:alpine,
    #    SITES_HOST_ROOT, BUILDS_ROOT, DOCKER_GID, S3_BUCKET, S3_REGION, API_DOMAIN)
    # 2. docker compose -f infra/docker-compose.dev.yml up -d
    #    (migrate применит alembic upgrade head; minio-setup создаст бакет)
    # 3. python -m app.db.seed   # сидит S1-пользователя с SEED_API_KEY
    # 4. Прогон pipeline (curl-последовательность ниже).

Ожидаемый результат: финальный GET /jobs/{id} → state=LIVE + live_url; GET {live_url}
отдаёт HTTP 200 (реально собранный Vite-сайт).

curl-последовательность (E2E_BASE_URL=http://api.localhost или http://localhost:8000):
    KEY=$SEED_API_KEY
    # POST /v1/projects → job_id
    curl -sX POST $BASE/v1/projects -H "Authorization: Bearer $KEY" \
         -H "Idempotency-Key: e2e-1" -H 'Content-Type: application/json' \
         -d '{"prompt":"A one-page landing site for a coffee shop"}'
    # poll GET /v1/jobs/{id} до state=AWAITING_CLARIFICATION
    # GET /v1/jobs/{id}/questions
    # POST /v1/jobs/{id}/answers с ответами на все question_id
    # poll GET /v1/jobs/{id} до state=LIVE (или FAILED)
    # GET {live_url} → ожидается HTTP 200
"""

from __future__ import annotations

import os
import time

import pytest

E2E_BASE_URL = os.environ.get("E2E_BASE_URL")
E2E_API_KEY = os.environ.get("E2E_API_KEY") or os.environ.get("SEED_API_KEY")
_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get(
    "ANTHROPIC_API_KEY"
) not in ("", "test-key", "test-anthropic-key")

requires_real_stack = pytest.mark.skipif(
    not (E2E_BASE_URL and E2E_API_KEY and _HAS_KEY),
    reason=(
        "Реальный E2E требует окружения: E2E_BASE_URL + (SEED_API_KEY|E2E_API_KEY) + "
        "настоящий ANTHROPIC_API_KEY + поднятый dev-стек (docker compose). "
        "См. docstring модуля для инструкции запуска."
    ),
)


@requires_real_stack
def test_prompt_to_live_url_real_stack():
    import httpx

    base = E2E_BASE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {E2E_API_KEY}"}

    with httpx.Client(base_url=base, timeout=30.0) as c:
        # 1. POST /projects
        r = c.post(
            "/v1/projects",
            data={"prompt": "A one-page landing site for a coffee shop"},
            headers={**headers, "Idempotency-Key": f"e2e-{int(time.time())}"},
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        # 2. poll → AWAITING_CLARIFICATION
        state = _poll_state(
            c, job_id, headers, target={"AWAITING_CLARIFICATION", "FAILED"}, timeout=180
        )
        assert state == "AWAITING_CLARIFICATION", f"interview не дошёл: {state}"

        # 3. GET questions
        rq = c.get(f"/v1/jobs/{job_id}/questions", headers=headers)
        assert rq.status_code == 200
        questions = rq.json()["questions"]
        assert questions

        # 4. POST answers (отвечаем на все)
        answers = [{"question_id": q["id"], "text": "Use sensible defaults."} for q in questions]
        ra = c.post(f"/v1/jobs/{job_id}/answers", json={"answers": answers}, headers=headers)
        assert ra.status_code == 202, ra.text

        # 5. poll → LIVE
        state = _poll_state(c, job_id, headers, target={"LIVE", "FAILED"}, timeout=600)
        assert state == "LIVE", f"pipeline не дошёл до LIVE: {state}"

        rj = c.get(f"/v1/jobs/{job_id}", headers=headers)
        live_url = rj.json()["live_url"]
        assert live_url, "live_url пуст при LIVE"

        # 6. GET live_url → 200 (реально собранный сайт).
        # verify=False осознанно: dev real-stack отдаёт сайт за Traefik на
        # *.apps.localhost без валидного TLS-сертификата; это health-check
        # сгенерированного сайта в dev-окружении, не прод. См. docstring модуля.
        site = httpx.get(live_url, timeout=30.0, verify=False)  # noqa: S501 - dev real-stack, self-signed/no TLS
        assert site.status_code == 200, f"сайт не отдаёт 200: {site.status_code}"


def _poll_state(client, job_id, headers, *, target, timeout):  # noqa: ANN001, ANN202
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/v1/jobs/{job_id}", headers=headers)
        r.raise_for_status()
        last = r.json()["state"]
        if last in target:
            return last
        time.sleep(3)
    return last
