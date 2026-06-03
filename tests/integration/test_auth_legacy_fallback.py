"""Integration: legacy fallback S1 (seeded users.api_key_hash) на время миграции.

ADR-008 «Миграционный путь», docs/modules/auth/03-architecture.md §3.
Seeded-ключ S1 (без lv_) продолжает аутентифицировать обычные endpoints, но управление
токенами (GET/DELETE /auth/tokens) требует строку api_tokens → legacy-ключ → 401.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("flush_redis")]


async def test_legacy_seeded_key_authenticates(client, auth_headers, seeded_user):
    """Seeded S1-ключ (без префикса lv_) → legacy fallback → 200 на /projects."""
    resp = await client.get("/v1/projects", headers=auth_headers)
    assert resp.status_code == 200


async def test_legacy_key_cannot_list_tokens(client, auth_headers, seeded_user):
    """Legacy-ключ не имеет строки api_tokens → GET /auth/tokens → 401 (нет current_token)."""
    resp = await client.get("/v1/auth/tokens", headers=auth_headers)
    assert resp.status_code == 401


async def test_legacy_key_cannot_revoke_tokens(client, auth_headers, seeded_user):
    """Legacy-ключ → DELETE /auth/tokens/{id} → 401 (управление токенами — только новый формат)."""
    resp = await client.delete("/v1/auth/tokens/t_anything0000000000000000", headers=auth_headers)
    assert resp.status_code == 401
