"""Integration: POST /v1/auth/apple + GET/DELETE /v1/auth/tokens (HTTP-уровень).

docs/modules/auth/02-api-contracts.md, 03-architecture.md, ADR-007/008,
docs/06-testing-strategy §Sprint 3. Реальный Postgres + Redis; внешняя граница JWKS Apple
изолирована (patch_apple_jwks — сеть НЕ вызывается). flush_redis изолирует rate-limit bucket.

Покрывает:
- happy upsert по apple_sub (новый user → adapty_customer_user_id=user.id; повтор → тот же
  user, новая строка api_tokens); 401 негативов верификации; 422 без identity_token;
- индексируемый lookup (валидный lv_-ключ → 200);
- мульти-устройство (N токенов, GET /tokens — только активные, current=true для текущего);
- DELETE → 204, идемпотентность повтора, чужой → 404, отозванный ключ → 401;
- cross-tenant изоляция токенов.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("patch_apple_jwks", "flush_redis")]


async def _sign_in(client, token: str, **body):  # noqa: ANN001, ANN201
    payload = {"identity_token": token, **body}
    return await client.post("/v1/auth/apple", json=payload)


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# --- POST /auth/apple: happy upsert ---


async def test_apple_sign_in_new_user_issues_key(client, session, make_apple_token):
    resp = await _sign_in(client, make_apple_token(sub="apple-new-1"), device_label="iPhone 15")
    assert resp.status_code == 200
    data = resp.json()
    assert data["api_key"].startswith("lv_")
    assert data["token_id"].startswith("t_")
    assert data["user_id"].startswith("u_")

    # adapty_customer_user_id = user.id при создании (docs §auth, ADR-007).
    from app.db.models import User

    user = await session.get(User, data["user_id"])
    assert user is not None
    assert user.apple_sub == "apple-new-1"
    assert user.adapty_customer_user_id == user.id


async def test_apple_sign_in_returned_key_authenticates(client, make_apple_token):
    resp = await _sign_in(client, make_apple_token(sub="apple-auth-1"))
    api_key = resp.json()["api_key"]
    # Индексируемый lookup: валидный lv_-ключ → 200.
    got = await client.get("/v1/auth/tokens", headers=_bearer(api_key))
    assert got.status_code == 200


async def test_apple_sign_in_repeat_same_user_new_token_row(client, session, make_apple_token):
    """Повторный вход тем же apple_sub → ТОТ ЖЕ user, НОВАЯ строка api_tokens (мульти-устр.)."""
    r1 = await _sign_in(client, make_apple_token(sub="apple-repeat"), device_label="iPhone")
    r2 = await _sign_in(client, make_apple_token(sub="apple-repeat"), device_label="iPad")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["user_id"] == r2.json()["user_id"]  # тот же user
    assert r1.json()["token_id"] != r2.json()["token_id"]  # разные токены

    from sqlalchemy import func, select

    from app.db.models import ApiToken

    count = await session.scalar(
        select(func.count()).select_from(ApiToken).where(ApiToken.user_id == r1.json()["user_id"])
    )
    assert count == 2  # две строки на одного user


# --- POST /auth/apple: негативы ---


async def test_apple_sign_in_bad_audience_returns_401(client, make_apple_token):
    resp = await _sign_in(client, make_apple_token(aud="wrong.bundle"))
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    # Не раскрываем конкретную проверку.
    assert "aud" not in resp.json()["detail"].lower()


async def test_apple_sign_in_bad_issuer_returns_401(client, make_apple_token):
    resp = await _sign_in(client, make_apple_token(iss="https://evil.example.com"))
    assert resp.status_code == 401


async def test_apple_sign_in_expired_returns_401(client, make_apple_token):
    resp = await _sign_in(client, make_apple_token(exp_offset_s=-3600, iat_offset_s=-7200))
    assert resp.status_code == 401


async def test_apple_sign_in_nonce_mismatch_returns_401(client, make_apple_token):
    resp = await _sign_in(client, make_apple_token(nonce="token-nonce"), nonce="other-nonce")
    assert resp.status_code == 401


async def test_apple_sign_in_bad_signature_returns_401(client, make_apple_token):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    foreign_pem = foreign.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    resp = await _sign_in(client, make_apple_token(sign_with=foreign_pem))
    assert resp.status_code == 401


async def test_apple_sign_in_missing_identity_token_returns_422(client):
    resp = await client.post("/v1/auth/apple", json={"nonce": "n"})
    assert resp.status_code == 422


# --- GET /auth/tokens: мульти-устройство + current flag ---


async def test_list_tokens_marks_current(client, make_apple_token):
    # Два устройства одного пользователя.
    r1 = await _sign_in(client, make_apple_token(sub="apple-multi"), device_label="iPhone")
    r2 = await _sign_in(client, make_apple_token(sub="apple-multi"), device_label="iPad")
    key2 = r2.json()["api_key"]
    tok1_id = r1.json()["token_id"]
    tok2_id = r2.json()["token_id"]

    # Запрос ключом второго устройства → current=true только у него.
    resp = await client.get("/v1/auth/tokens", headers=_bearer(key2))
    assert resp.status_code == 200
    tokens = {t["id"]: t for t in resp.json()["tokens"]}
    assert set(tokens) == {tok1_id, tok2_id}  # обе активны
    assert tokens[tok2_id]["current"] is True
    assert tokens[tok1_id]["current"] is False
    # key_id показываем (не секрет), secret/hash — никогда.
    for t in tokens.values():
        assert "key_id" in t
        assert "secret" not in t and "key_hash" not in t


# --- DELETE /auth/tokens/{id}: revoke, идемпотентность, cross-tenant, 401-после-revoke ---


async def test_revoke_token_returns_204_then_key_401(client, make_apple_token):
    r1 = await _sign_in(client, make_apple_token(sub="apple-revoke"), device_label="iPhone")
    r2 = await _sign_in(client, make_apple_token(sub="apple-revoke"), device_label="iPad")
    key1, tok1_id = r1.json()["api_key"], r1.json()["token_id"]
    key2 = r2.json()["api_key"]

    # Отзываем устройство 1 ключом устройства 2.
    d = await client.delete(f"/v1/auth/tokens/{tok1_id}", headers=_bearer(key2))
    assert d.status_code == 204
    # Повтор по отозванному → 204 (идемпотентно).
    d2 = await client.delete(f"/v1/auth/tokens/{tok1_id}", headers=_bearer(key2))
    assert d2.status_code == 204
    # Отозванный ключ устройства 1 → 401.
    after = await client.get("/v1/auth/tokens", headers=_bearer(key1))
    assert after.status_code == 401


async def test_revoke_foreign_token_returns_404(client, make_apple_token):
    a = await _sign_in(client, make_apple_token(sub="apple-A"))
    b = await _sign_in(client, make_apple_token(sub="apple-B"))
    a_key = a.json()["api_key"]
    b_tok = b.json()["token_id"]
    # A отзывает токен B → 404 (cross-tenant, не раскрываем существование).
    resp = await client.delete(f"/v1/auth/tokens/{b_tok}", headers=_bearer(a_key))
    assert resp.status_code == 404


async def test_user_a_cannot_see_user_b_tokens(client, make_apple_token):
    a = await _sign_in(client, make_apple_token(sub="apple-iso-A"))
    await _sign_in(client, make_apple_token(sub="apple-iso-B"))
    a_key = a.json()["api_key"]
    a_tok = a.json()["token_id"]
    resp = await client.get("/v1/auth/tokens", headers=_bearer(a_key))
    ids = {t["id"] for t in resp.json()["tokens"]}
    assert ids == {a_tok}  # только свой токен


async def test_delete_nonexistent_token_returns_404(client, make_apple_token):
    a = await _sign_in(client, make_apple_token(sub="apple-del404"))
    a_key = a.json()["api_key"]
    resp = await client.delete("/v1/auth/tokens/t_doesnotexist000000000000", headers=_bearer(a_key))
    assert resp.status_code == 404
