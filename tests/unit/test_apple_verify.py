"""Unit: верификация Apple identity token (mock JWKS, сеть НЕ вызывается).

docs/modules/auth/03-architecture.md §1, ADR-007, docs/06-testing-strategy §Auth.
Покрывает happy verify (возврат sub) и все негативы → AppleTokenError (наружу 401,
без раскрытия конкретной непрошедшей проверки): подпись/iss/aud/exp/nonce/kid/sub.
Внешняя граница JWKS изолирована фикстурой patch_apple_jwks (fetch_jwks не вызывается).
"""

from __future__ import annotations

import pytest

from app.auth.apple_verify import AppleTokenError, verify_apple_identity_token

# --- happy path ---


def test_valid_token_returns_sub(patch_apple_jwks, make_apple_token):
    token = make_apple_token(sub="apple-sub-happy")
    assert verify_apple_identity_token(token) == "apple-sub-happy"


def test_valid_token_with_matching_nonce(patch_apple_jwks, make_apple_token):
    token = make_apple_token(sub="s1", nonce="n-good")
    assert verify_apple_identity_token(token, nonce="n-good") == "s1"


def test_verification_is_offline_no_network(patch_apple_jwks, make_apple_token):
    """Верификация офлайн: только get_signing_key (кэш), fetch_jwks (сеть) не дёргается."""
    token = make_apple_token(sub="s-off")
    verify_apple_identity_token(token)
    assert patch_apple_jwks["get_signing_key"] == 1  # ровно один lookup ключа по kid


# --- негативы → AppleTokenError (единый 401, без деталей) ---


def test_bad_signature_rejected(patch_apple_jwks, make_apple_token):
    """Токен подписан ЧУЖИМ ключом → подпись не сходится с тест-JWKS → AppleTokenError."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    foreign_pem = foreign.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    token = make_apple_token(sub="s", sign_with=foreign_pem)
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token)


def test_wrong_issuer_rejected(patch_apple_jwks, make_apple_token):
    token = make_apple_token(iss="https://evil.example.com")
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token)


def test_wrong_audience_rejected(patch_apple_jwks, make_apple_token):
    token = make_apple_token(aud="some.other.bundle")
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token)


def test_expired_token_rejected(patch_apple_jwks, make_apple_token):
    # exp далеко в прошлом (за пределами leeway).
    token = make_apple_token(exp_offset_s=-3600, iat_offset_s=-7200)
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token)


def test_nonce_mismatch_rejected(patch_apple_jwks, make_apple_token):
    token = make_apple_token(nonce="token-nonce")
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token, nonce="expected-different-nonce")


def test_nonce_expected_but_absent_in_token_rejected(patch_apple_jwks, make_apple_token):
    """Клиент передал nonce, но в токене его нет → mismatch → AppleTokenError."""
    token = make_apple_token()  # без nonce-claim
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token, nonce="expected")


def test_missing_kid_header_rejected(patch_apple_jwks, apple_rsa_keypair):
    """Заголовок без kid → нечего искать в JWKS → AppleTokenError (до verify)."""
    import jwt

    private_pem, _ = apple_rsa_keypair
    token = jwt.encode(
        {"iss": "https://appleid.apple.com", "aud": "x", "sub": "s", "iat": 1, "exp": 9999999999},
        private_pem,
        algorithm="RS256",  # без headers={"kid": ...} → kid отсутствует
    )
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token)


def test_malformed_token_rejected(patch_apple_jwks):
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token("not-a-jwt")


def test_missing_sub_claim_rejected(patch_apple_jwks, make_apple_token):
    token = make_apple_token(omit_sub=True)
    with pytest.raises(AppleTokenError):
        verify_apple_identity_token(token)
