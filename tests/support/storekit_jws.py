"""Тестовые фикстуры StoreKit JWS (ADR-039): генерация EC cert-chain + подпись ES256 JWS.

Воспроизводит формат Apple signed StoreKit 2 JWS-транзакции ДЛЯ ТЕСТОВ (собственная тест-пара
root+leaf, НЕ реальный Apple root):
  - `make_root()` — самоподписанный EC P-256 root CA (кладётся в APPSTORE_ROOT_CERT_DIR теста).
  - `make_leaf(root)` — leaf-сертификат, подписанный приватным ключом root.
  - `sign_jws(payload, leaf, chain)` — компактный ES256 JWS, header несёт x5c=[leaf, root]
    (стандартный base64 DER), подпись приватным ключом leaf.
  - `build_transaction_jws(...)` — сквозной билдер валидной транзакции + запись root в каталог.

Верификатор (app/billing/storekit) проверяет: alg=ES256 + x5c → цепочка терминируется в
доверенном root (по DER-отпечатку) → ES256-подпись leaf-ключом → payload. Тест-хелперы дают
управляемо-валидные И управляемо-невалидные (не тот root / подделанный payload / нет x5c /
alg≠ES256) входы для fail-closed-сценариев.
"""

from __future__ import annotations

import base64
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_P256 = ec.SECP256R1()


@dataclass
class CertKey:
    """Пара сертификат + приватный ключ (EC P-256)."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def der(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.DER)

    @property
    def key_pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def make_root(cn: str = "Test Apple Root CA G-Test") -> CertKey:
    """Самоподписанный EC P-256 root CA (аналог Apple production root для тестов)."""
    key = ec.generate_private_key(_P256)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - _dt.timedelta(days=1))
        .not_valid_after(_now() + _dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return CertKey(cert=cert, key=key)


def make_leaf(root: CertKey, cn: str = "Test Apple StoreKit Leaf") -> CertKey:
    """Leaf-сертификат, подписанный приватным ключом `root` (issuer = root.subject)."""
    key = ec.generate_private_key(_P256)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(root.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - _dt.timedelta(days=1))
        .not_valid_after(_now() + _dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(root.key, hashes.SHA256())
    )
    return CertKey(cert=cert, key=key)


def _x5c(chain: list[CertKey]) -> list[str]:
    """x5c-заголовок: стандартный base64 DER каждого сертификата (не base64url)."""
    return [base64.b64encode(c.der).decode("ascii") for c in chain]


def sign_jws(
    payload: dict[str, Any],
    leaf: CertKey,
    chain: list[CertKey],
    *,
    algorithm: str = "ES256",
    include_x5c: bool = True,
) -> str:
    """Компактный JWS: подпись leaf-ключом (ES256), header x5c=цепочка (по умолчанию)."""
    headers: dict[str, Any] = {}
    if include_x5c:
        headers["x5c"] = _x5c(chain)
    return jwt.encode(payload, leaf.key_pem, algorithm=algorithm, headers=headers)


def transaction_payload(
    *,
    transaction_id: str,
    product_id: str,
    original_transaction_id: str | None = None,
    bundle_id: str = "mba.gipsy.lovable",
    environment: str = "Xcode",
    expires_ms: int | None = None,
    revoked: bool = False,
) -> dict[str, Any]:
    """Payload Apple JWSTransaction (поля, читаемые верификатором)."""
    payload: dict[str, Any] = {
        "transactionId": transaction_id,
        "originalTransactionId": original_transaction_id or transaction_id,
        "productId": product_id,
        "bundleId": bundle_id,
        "environment": environment,
    }
    if expires_ms is not None:
        payload["expiresDate"] = expires_ms
    if revoked:
        payload["revocationDate"] = int(_now().timestamp() * 1000)
    return payload


def write_roots(cert_dir: Path, roots: list[CertKey]) -> Path:
    """Пишет DER-сертификаты roots в каталог (.cer) — тест-APPSTORE_ROOT_CERT_DIR."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(roots):
        (cert_dir / f"root_{i}.cer").write_bytes(r.der)
    return cert_dir


def build_transaction_jws(
    cert_dir: Path,
    *,
    transaction_id: str,
    product_id: str,
    original_transaction_id: str | None = None,
    bundle_id: str = "mba.gipsy.lovable",
    environment: str = "Xcode",
    expires_ms: int | None = None,
    revoked: bool = False,
    trust_root: bool = True,
) -> str:
    """Сквозной билдер: root+leaf, JWS (x5c=[leaf, root]); root пишется в cert_dir при trust_root.

    trust_root=False → root в каталог НЕ пишется (цепочка не терминируется в доверенном root).
    """
    root = make_root()
    leaf = make_leaf(root)
    if trust_root:
        write_roots(cert_dir, [root])
    payload = transaction_payload(
        transaction_id=transaction_id,
        product_id=product_id,
        original_transaction_id=original_transaction_id,
        bundle_id=bundle_id,
        environment=environment,
        expires_ms=expires_ms,
        revoked=revoked,
    )
    return sign_jws(payload, leaf, [leaf, root])
