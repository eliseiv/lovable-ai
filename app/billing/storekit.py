"""Верификатор Apple signed StoreKit 2 JWS-транзакции (ADR-039 §A, docs billing §13.1).

Собственная криптографическая верификация подписанной StoreKit 2 JWS-транзакции (fail-closed):
заголовок JWS несёт цепочку сертификатов x5c; проверяем цепочку до доверенного Apple root
(загружается из APPSTORE_ROOT_CERT_DIR), затем ES256-подпись JWS публичным ключом
leaf-сертификата (PyJWT[crypto]), затем валидируем декодированный payload (bundleId,
environment, revocationDate). Работает офлайн (без Apple/Adapty) во всех окружениях
(Xcode StoreKit Testing + Sandbox + Production).

**Payload/JWS НЕ логируются** (docs/05-security.md → StoreKit): в логи/Sentry идут максимум
transaction_id/environment, никогда — тело транзакции или сам JWS.

**Fail-closed (ADR-039 §C, docs/05-security):** APPSTORE_ROOT_CERT_DIR не сконфигурирован
(пусто/каталог нет/без сертификатов) → цепочку верифицировать нечем → верификатор
отказывается пометить транзакцию верифицированной (StoreKitVerificationError → 422),
а не принимает неверифицируемую транзакцию. Верификатор чист от БД (только крипто).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Расширения файлов доверенных root-сертификатов в APPSTORE_ROOT_CERT_DIR (DER/PEM).
_CERT_SUFFIXES: frozenset[str] = frozenset({".cer", ".der", ".pem", ".crt"})


class StoreKitVerificationError(Exception):
    """Отказ верификации StoreKit JWS (роутер → 422 invalid-storekit-transaction, ADR-039 §B).

    Поднимается на ЛЮБОМ шаге fail-closed: невалидный JWS/заголовок, отсутствие x5c, roots не
    сконфигурированы, цепочка не терминируется в доверенном Apple root, невалидная ES256-подпись,
    bundle mismatch, отсутствие transactionId. Крипто-детали в теле ответа не раскрываются.
    """


@dataclass(frozen=True)
class VerifiedTransaction:
    """Нормализованная верифицированная StoreKit-транзакция (ADR-039 §A.6, docs §13.1)."""

    transaction_id: str
    original_transaction_id: str
    product_id: str
    expires_at: datetime | None
    revoked: bool
    environment: str


def _b64url_decode(segment: str) -> bytes:
    """base64url-декод сегмента JWS (с добавлением padding)."""
    padding_chars = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding_chars)


def _jws_header(jws: str) -> dict[str, Any]:
    """Декодированный заголовок JWS (первый сегмент). Не-объект/не-base64url → отказ."""
    header_segment = jws.split(".", 1)[0]
    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, json.JSONDecodeError) as exc:
        raise StoreKitVerificationError("StoreKit JWS header is not valid base64url JSON") from exc
    if not isinstance(header, dict):
        raise StoreKitVerificationError("StoreKit JWS header must be a JSON object")
    return header


def _load_certificate_chain(jws: str) -> list[x509.Certificate]:
    """Цепочка сертификатов x5c (base64 DER) из заголовка JWS. Нет x5c/не-список → отказ."""
    header = _jws_header(jws)
    if str(header.get("alg", "")) != "ES256":
        # alg ≠ ES256 → отказ (ADR-039 §A.1): принимаем только Apple ES256 JWS.
        raise StoreKitVerificationError("StoreKit JWS alg must be ES256")
    x5c = header.get("x5c")
    if not x5c or not isinstance(x5c, list):
        raise StoreKitVerificationError("StoreKit JWS missing x5c certificate chain")
    try:
        return [x509.load_der_x509_certificate(base64.b64decode(cert)) for cert in x5c]
    except (ValueError, TypeError) as exc:
        raise StoreKitVerificationError("StoreKit JWS x5c is not a valid DER chain") from exc


def _verify_signed_by(child: x509.Certificate, issuer: x509.Certificate) -> None:
    """Проверяет, что `child` подписан приватным ключом `issuer` (public-key verify)."""
    hash_alg = child.signature_hash_algorithm
    if hash_alg is None:
        raise StoreKitVerificationError("certificate missing signature hash algorithm")
    pubkey = issuer.public_key()
    if isinstance(pubkey, ec.EllipticCurvePublicKey):
        pubkey.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(hash_alg))
    elif isinstance(pubkey, rsa.RSAPublicKey):
        pubkey.verify(child.signature, child.tbs_certificate_bytes, padding.PKCS1v15(), hash_alg)
    else:  # pragma: no cover - Apple использует EC; дефенсив
        raise StoreKitVerificationError("Unsupported certificate key type in StoreKit chain")


def _verify_chain(chain: list[x509.Certificate], roots: list[x509.Certificate]) -> None:
    """Каждый cert подписан следующим; цепочка терминируется в доверенном root (ADR-039 §A.3).

    Верифицирует подпись каждого звена публичным ключом вышестоящего, затем требует, чтобы
    вершина цепочки совпадала с доверенным root (по DER-отпечатку) ЛИБО была им подписана.
    Не терминируется в доверенном root → отказ.
    """
    for i in range(len(chain) - 1):
        try:
            _verify_signed_by(chain[i], chain[i + 1])
        except StoreKitVerificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - InvalidSignature/ValueError/TypeError → fail-closed StoreKitVerificationError (крипто-детали не раскрываем, 05-security)
            raise StoreKitVerificationError(
                "StoreKit certificate chain link signature invalid"
            ) from exc

    root_in_chain = chain[-1]
    root_fingerprints = {r.public_bytes(Encoding.DER) for r in roots}
    if root_in_chain.public_bytes(Encoding.DER) in root_fingerprints:
        return
    # Вершина цепочки не является доверенным root напрямую — проверяем, что она им подписана.
    for trusted in roots:
        try:
            _verify_signed_by(root_in_chain, trusted)
            return
        except Exception:  # noqa: BLE001, S112 - пробуем следующий доверенный root (крипто-исключение не логируем, 05-security)
            continue
    raise StoreKitVerificationError("StoreKit certificate chain not anchored to a trusted root")


def _decode_unverified_payload(signed_transaction: str) -> dict[str, Any]:
    """Payload JWS БЕЗ проверки подписи — только для тестового обхода (ADR-043).

    Используется исключительно при STOREKIT_INSECURE_SKIP_VERIFY=true. Невалидный
    base64url/JSON или не-объект → StoreKitVerificationError (структурный fail остаётся).
    """
    try:
        payload = jwt.decode(
            signed_transaction,
            options={"verify_signature": False, "verify_aud": False, "verify_exp": False},
            algorithms=["ES256"],
        )
    except jwt.InvalidTokenError as exc:
        raise StoreKitVerificationError("StoreKit JWS payload is not decodable") from exc
    if not isinstance(payload, dict):
        raise StoreKitVerificationError("StoreKit JWS payload must be a JSON object")
    return payload


class StoreKitVerifier:
    """Верифицирует Apple-подписанные StoreKit JWS-транзакции (ES256 + x5c → Apple root)."""

    def __init__(self, settings: Settings) -> None:
        self._bundle_id = settings.appstore_bundle_id
        self._roots = self._load_roots(settings.appstore_root_cert_dir)
        # ⚠️ ВРЕМЕННЫЙ ТЕСТОВЫЙ ОБХОД (ADR-043): пропуск крипто-проверки JWS на тест-инстансе.
        self._skip_verify = settings.storekit_insecure_skip_verify
        if self._skip_verify:
            logger.warning("storekit_signature_verification_disabled")

    @staticmethod
    def _load_roots(cert_dir: str) -> list[x509.Certificate]:
        """Загрузка доверенных roots из каталога (DER, fallback PEM). Пусто/нет → []."""
        if not cert_dir:
            return []
        directory = Path(cert_dir)
        if not directory.is_dir():
            return []
        roots: list[x509.Certificate] = []
        for path in sorted(directory.glob("*")):
            if path.suffix.lower() not in _CERT_SUFFIXES:
                continue
            data = path.read_bytes()
            try:
                roots.append(x509.load_der_x509_certificate(data))
            except ValueError:
                roots.append(x509.load_pem_x509_certificate(data))
        return roots

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        """Верифицирует одну подписанную JWS-транзакцию → нормализованные поля (ADR-039 §A).

        Fail-closed: любой сбой шага → StoreKitVerificationError (роутер → 422). Roots не
        сконфигурированы → отказ (не начисляем на неверифицируемой транзакции). Payload/JWS
        НЕ логируются.
        """
        if not isinstance(signed_transaction, str) or signed_transaction.count(".") != 2:
            raise StoreKitVerificationError("StoreKit transaction must be a compact JWS string")

        if self._skip_verify:
            # ⚠️ ВРЕМЕННЫЙ ТЕСТОВЫЙ ОБХОД (ADR-043, STOREKIT_INSECURE_SKIP_VERIFY=true):
            # цепочка и подпись НЕ проверяются, payload берётся как есть. Структурная
            # валидация (_normalize_payload: bundleId/transactionId) сохраняется.
            logger.warning("storekit_verification_bypassed")
            return self._normalize_payload(_decode_unverified_payload(signed_transaction))

        chain = _load_certificate_chain(signed_transaction)
        leaf = chain[0]

        if not self._roots:
            # Нет доверенного якоря (APPSTORE_ROOT_CERT_DIR) — цепочку верифицировать нечем.
            raise StoreKitVerificationError(
                "App Store root certificates not configured (APPSTORE_ROOT_CERT_DIR); "
                "cannot verify StoreKit transaction"
            )
        _verify_chain(chain, self._roots)

        leaf_pubkey = leaf.public_key()
        try:
            payload: dict[str, Any] = jwt.decode(
                signed_transaction,
                key=leaf_pubkey,  # type: ignore[arg-type]
                algorithms=["ES256"],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError as exc:
            raise StoreKitVerificationError("StoreKit JWS signature invalid") from exc

        return self._normalize_payload(payload)

    def _normalize_payload(self, payload: dict[str, Any]) -> VerifiedTransaction:
        """Валидация + нормализация payload верифицированной транзакции (ADR-039 §A.5/§A.6).

        bundleId сверяется только при непустом APPSTORE_BUNDLE_ID (пусто → skip, тест/dev).
        environment фиксируется как есть (Xcode/Sandbox/Production — сохраняем регистр Apple,
        docs §13.4 разграничение каналов ключуется по environment). revocationDate → revoked.
        Нет transactionId → отказ.
        """
        bundle_id = payload.get("bundleId")
        if self._bundle_id and bundle_id != self._bundle_id:
            raise StoreKitVerificationError("StoreKit transaction bundleId mismatch")

        if "transactionId" not in payload:
            raise StoreKitVerificationError("StoreKit transaction missing transactionId")

        environment = str(payload.get("environment", ""))

        expires_ms = payload.get("expiresDate")
        expires_at = (
            datetime.fromtimestamp(int(expires_ms) / 1000, tz=UTC)
            if expires_ms is not None
            else None
        )
        revoked = payload.get("revocationDate") is not None

        transaction_id = str(payload["transactionId"])
        return VerifiedTransaction(
            transaction_id=transaction_id,
            original_transaction_id=str(payload.get("originalTransactionId", transaction_id)),
            product_id=str(payload.get("productId", "")),
            expires_at=expires_at,
            revoked=revoked,
            environment=environment,
        )


_verifier_singleton: StoreKitVerifier | None = None


def get_storekit_verifier() -> StoreKitVerifier:
    """Процесс-синглтон верификатора (roots грузятся один раз из APPSTORE_ROOT_CERT_DIR)."""
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = StoreKitVerifier(get_settings())
    return _verifier_singleton
