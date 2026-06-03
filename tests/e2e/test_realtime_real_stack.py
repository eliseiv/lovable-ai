"""РЕАЛЬНЫЙ E2E Sprint 5 (SSE + APNs) на живом стеке — SKIP без окружения (ADR-012/013).

Эти приёмочные пункты требуют ЖИВОГО окружения и НЕ автоматизируются в CI:
  - SSE на живом API за реальным reverse-proxy (буферизация/таймауты прокси, EventSource
    клиента, reconnect через Last-Event-ID на реальном TCP-разрыве);
  - APNs push на боевой/sandbox APNs Provider API с настоящим .p8-ключом (Apple Developer):
    HTTP/2 POST /3/device, ES256 provider-JWT, реальные 200/410/429 от Apple.

Без окружения тест SKIP (не выдумывает результат — правило qa). Автоматизируемое покрыто
unit/integration с моками внешних границ (test_sse_events, test_apns_push, test_apns_send).

Инструкция запуска SSE:
    # dev-стек поднят (см. test_real_stack_e2e), затем:
    #   curl -N $BASE/v1/jobs/{jid}/events -H "Authorization: Bearer $KEY"
    #   (ожидается: снимок+retry → live-события → event: done на терминале)
Инструкция запуска APNs:
    # .p8-ключ + APNS_KEY_ID/APNS_TEAM_ID/APNS_BUNDLE_ID/APNS_ENV в .env воркера,
    # зарегистрировать устройство POST /v1/devices, прогнать джобу до LIVE/FAILED,
    # проверить доставку push на устройство.
"""

from __future__ import annotations

import os

import pytest

E2E_BASE_URL = os.environ.get("E2E_BASE_URL")
E2E_API_KEY = os.environ.get("E2E_API_KEY") or os.environ.get("SEED_API_KEY")
_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get(
    "ANTHROPIC_API_KEY"
) not in ("", "test-key", "test-anthropic-key")

requires_real_stack = pytest.mark.skipif(
    not (E2E_BASE_URL and E2E_API_KEY and _HAS_KEY),
    reason=(
        "Реальный E2E Sprint 5 (SSE/APNs) требует живого стека: E2E_BASE_URL + "
        "(SEED_API_KEY|E2E_API_KEY) + настоящий ANTHROPIC_API_KEY. APNs дополнительно — "
        ".p8-ключ + APNS_* (Apple Developer). См. docstring модуля. Автоматизируемое покрыто "
        "моками (test_sse_events/test_apns_push/test_apns_send)."
    ),
)


@requires_real_stack
def test_sse_stream_real_stack():  # pragma: no cover - живой стек
    import time

    import httpx

    base = E2E_BASE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {E2E_API_KEY}"}
    with httpx.Client(base_url=base, timeout=30.0) as c:
        r = c.post(
            "/v1/projects",
            json={"prompt": "A one-page landing site"},
            headers={**headers, "Idempotency-Key": f"sse-e2e-{int(time.time())}"},
        )
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        # Стрим: первый кадр — снимок + retry, далее live-события до done.
        with c.stream("GET", f"/v1/jobs/{job_id}/events", headers=headers) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            saw_retry = False
            for line in resp.iter_lines():
                if line.startswith("retry:"):
                    saw_retry = True
                if line.startswith("event: done"):
                    break
            assert saw_retry


@requires_real_stack
def test_apns_push_real_stack():  # pragma: no cover - живой APNs
    pytest.skip(
        "APNs живой push проверяется вручную с .p8-ключом Apple Developer на устройстве — "
        "автоматизируемое покрыто mock-тестами (test_apns_send/test_apns_push)."
    )
