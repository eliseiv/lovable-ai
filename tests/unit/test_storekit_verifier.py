"""Unit: StoreKit JWS-верификатор (ADR-039 §A, app/billing/storekit) на тест-фикстурах.

Покрытие (docs/06-testing-strategy.md §Unit «StoreKit JWS-верификатор» + ADR-039 §A/§C/§H):
  - валидная ES256/x5c транзакция, цепочка → доверенный тест-root → VerifiedTransaction с
    корректными полями (transaction_id/original/product/expires_at/environment/revoked);
  - fail-closed без roots: пустой/несуществующий APPSTORE_ROOT_CERT_DIR → StoreKitVerificationError;
  - цепочка не терминируется в доверенном root → отказ;
  - подделанный payload (невалидная ES256-подпись) → отказ;
  - нет x5c / alg≠ES256 / не-JWS-строка → отказ;
  - bundle-check: mismatch при непустом APPSTORE_BUNDLE_ID → отказ; пустой bundle → skip;
  - Xcode-профиль: транзакция проходит ТОЛЬКО когда её root в trusted dir (косвенно — prod-инвариант
    «тест-сертификат не в roots → отказ»);
  - payload/JWS не логируются (caplog).
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app.billing.storekit import StoreKitVerificationError, StoreKitVerifier
from tests.support import storekit_jws as sk


def _settings(cert_dir: str, bundle_id: str = "", skip_verify: bool = False):  # noqa: ANN202
    """Минимальный стенд-ин Settings (верификатор читает только эти три поля)."""
    return SimpleNamespace(
        appstore_root_cert_dir=cert_dir,
        appstore_bundle_id=bundle_id,
        storekit_insecure_skip_verify=skip_verify,
    )


# ============================ валидная транзакция ============================


def test_valid_transaction_returns_verified_fields(tmp_path):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = sk.transaction_payload(
        transaction_id="2000000111",
        original_transaction_id="2000000100",
        product_id="250_tokens_19.99",
        environment="Sandbox",
        expires_ms=1893456000000,  # 2030-01-01
    )
    jws = sk.sign_jws(payload, leaf, [leaf, root])

    verifier = StoreKitVerifier(_settings(str(tmp_path)))
    txn = verifier.verify(jws)

    assert txn.transaction_id == "2000000111"
    assert txn.original_transaction_id == "2000000100"
    assert txn.product_id == "250_tokens_19.99"
    assert txn.environment == "Sandbox"
    assert txn.revoked is False
    assert txn.expires_at is not None
    assert txn.expires_at.year == 2030


def test_original_transaction_id_falls_back_to_transaction_id(tmp_path):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = {
        "transactionId": "777",
        "productId": "p",
        "bundleId": "b",
        "environment": "Xcode",
    }
    jws = sk.sign_jws(payload, leaf, [leaf, root])
    txn = StoreKitVerifier(_settings(str(tmp_path))).verify(jws)
    assert txn.original_transaction_id == "777"


def test_revoked_transaction_flagged(tmp_path):
    jws = sk.build_transaction_jws(tmp_path, transaction_id="rv1", product_id="p", revoked=True)
    txn = StoreKitVerifier(_settings(str(tmp_path))).verify(jws)
    assert txn.revoked is True


# ============================ fail-closed без roots ============================


def test_fail_closed_when_root_dir_empty(tmp_path):
    """Синтаксически валидная цепочка, но каталог roots пуст → отказ (не начисляем)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    # Собираем валидный JWS, но root в каталог НЕ кладём.
    jws = sk.build_transaction_jws(
        tmp_path / "certs", transaction_id="x", product_id="p", trust_root=False
    )
    verifier = StoreKitVerifier(_settings(str(empty_dir)))
    with pytest.raises(StoreKitVerificationError):
        verifier.verify(jws)


def test_fail_closed_when_root_dir_missing(tmp_path):
    """Несуществующий каталог → нет доверенных roots → отказ."""
    jws = sk.build_transaction_jws(
        tmp_path / "certs", transaction_id="x", product_id="p", trust_root=False
    )
    verifier = StoreKitVerifier(_settings(str(tmp_path / "does-not-exist")))
    with pytest.raises(StoreKitVerificationError):
        verifier.verify(jws)


def test_fail_closed_when_root_dir_unset(tmp_path):
    """Пустая строка APPSTORE_ROOT_CERT_DIR → отказ."""
    jws = sk.build_transaction_jws(
        tmp_path / "certs", transaction_id="x", product_id="p", trust_root=False
    )
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings("")).verify(jws)


# ============================ цепочка / подпись ============================


def test_chain_not_anchored_to_trusted_root_rejected(tmp_path):
    """Цепочка подписана rootA, а в trusted dir лежит несвязанный rootB → отказ."""
    root_a = sk.make_root("Root A")
    leaf = sk.make_leaf(root_a)
    root_b = sk.make_root("Root B (unrelated)")
    sk.write_roots(tmp_path, [root_b])  # доверяем ДРУГОМУ root
    payload = sk.transaction_payload(transaction_id="c1", product_id="p")
    jws = sk.sign_jws(payload, leaf, [leaf, root_a])
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)


def test_invalid_chain_link_signature_rejected(tmp_path):
    """Невалидное ЗВЕНО цепочки: leaf НЕ подписан следующим cert → отказ (fail-closed).

    x5c=[attacker_leaf, trusted_root]: цепочка присутствует, вершина = доверенный root (в
    trusted dir), НО звено leaf→root подписью не проходит (leaf подписан ЧУЖИМ attacker_root).
    Отказ возникает в ПОСЛЕДОВАТЕЛЬНОМ цикле `_verify_signed_by` (сообщение "chain link
    signature invalid"), а НЕ в anchor-проверке — `match` это фиксирует. ДО фикса ADR-039 этот
    путь бросал непойманный InvalidSignature → HTTP 500; после — StoreKitVerificationError → 422.
    """
    trusted_root = sk.make_root("Trusted Root")
    attacker_root = sk.make_root("Attacker Root")
    attacker_leaf = sk.make_leaf(attacker_root)  # подписан attacker_root, НЕ trusted_root
    sk.write_roots(tmp_path, [trusted_root])  # доверяем ИМЕННО trusted_root (anchor прошёл бы)
    payload = sk.transaction_payload(transaction_id="badlink", product_id="p")
    jws = sk.sign_jws(payload, attacker_leaf, [attacker_leaf, trusted_root])
    with pytest.raises(StoreKitVerificationError, match="link signature invalid"):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)


def test_tampered_payload_invalid_signature_rejected(tmp_path):
    """Подмена payload после подписи → ES256-подпись невалидна → отказ."""
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = sk.transaction_payload(transaction_id="orig", product_id="cheap")
    jws = sk.sign_jws(payload, leaf, [leaf, root])

    header_b64, payload_b64, sig_b64 = jws.split(".")
    forged = {"transactionId": "orig", "productId": "expensive_pack", "bundleId": "b"}
    forged_b64 = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode("ascii")
    tampered = f"{header_b64}.{forged_b64}.{sig_b64}"
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(tampered)


def test_missing_x5c_rejected(tmp_path):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = sk.transaction_payload(transaction_id="nox5c", product_id="p")
    jws = sk.sign_jws(payload, leaf, [leaf, root], include_x5c=False)
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)


def test_non_es256_alg_rejected(tmp_path):
    """alg≠ES256 (HS256) → отказ до крипто-проверки цепочки."""
    root = sk.make_root()
    sk.write_roots(tmp_path, [root])
    jws = jwt_hs256({"transactionId": "hs", "productId": "p", "bundleId": "b"})
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)


def test_not_a_compact_jws_rejected(tmp_path):
    root = sk.make_root()
    sk.write_roots(tmp_path, [root])
    verifier = StoreKitVerifier(_settings(str(tmp_path)))
    for bad in ["not-a-jws", "only.one-dot", "a.b.c.d"]:
        with pytest.raises(StoreKitVerificationError):
            verifier.verify(bad)


def jwt_hs256(payload: dict) -> str:  # noqa: ANN001
    import jwt

    return jwt.encode(payload, "x" * 32, algorithm="HS256")


# ============================ bundle-check ============================


def test_bundle_mismatch_rejected_when_bundle_configured(tmp_path):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = sk.transaction_payload(
        transaction_id="b1", product_id="p", bundle_id="com.evil.other"
    )
    jws = sk.sign_jws(payload, leaf, [leaf, root])
    verifier = StoreKitVerifier(_settings(str(tmp_path), bundle_id="mba.gipsy.lovable"))
    with pytest.raises(StoreKitVerificationError):
        verifier.verify(jws)


def test_bundle_match_accepted_when_bundle_configured(tmp_path):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = sk.transaction_payload(
        transaction_id="b2", product_id="p", bundle_id="mba.gipsy.lovable"
    )
    jws = sk.sign_jws(payload, leaf, [leaf, root])
    verifier = StoreKitVerifier(_settings(str(tmp_path), bundle_id="mba.gipsy.lovable"))
    txn = verifier.verify(jws)
    assert txn.transaction_id == "b2"


def test_bundle_check_skipped_when_bundle_empty(tmp_path):
    """Пустой APPSTORE_BUNDLE_ID → любой bundleId проходит (Xcode/тест)."""
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = sk.transaction_payload(
        transaction_id="b3", product_id="p", bundle_id="whatever.anything"
    )
    jws = sk.sign_jws(payload, leaf, [leaf, root])
    txn = StoreKitVerifier(_settings(str(tmp_path), bundle_id="")).verify(jws)
    assert txn.transaction_id == "b3"


def test_missing_transaction_id_rejected(tmp_path):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    payload = {"productId": "p", "bundleId": "b", "environment": "Xcode"}
    jws = sk.sign_jws(payload, leaf, [leaf, root])
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)


# ============================ Xcode-профиль / prod-инвариант ============================


def test_xcode_cert_accepted_only_when_in_trusted_roots(tmp_path):
    """Транзакция под тест-root проходит ТОЛЬКО когда этот root в APPSTORE_ROOT_CERT_DIR.

    Косвенно фиксирует prod-инвариант: тест-сертификат НЕ в trusted roots → Xcode-JWS отклонён.
    """
    xcode_root = sk.make_root("Xcode StoreKit Test Root")
    leaf = sk.make_leaf(xcode_root)
    payload = sk.transaction_payload(transaction_id="xc", product_id="p", environment="Xcode")
    jws = sk.sign_jws(payload, leaf, [leaf, xcode_root])

    # Каталог roots ПУСТ (эмуляция prod без тест-сертификата) → отказ.
    empty = tmp_path / "prod_roots"
    empty.mkdir()
    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(empty))).verify(jws)

    # Тот же JWS с тест-root в trusted dir (тест/dev-инстанс) → принят.
    dev = tmp_path / "dev_roots"
    sk.write_roots(dev, [xcode_root])
    txn = StoreKitVerifier(_settings(str(dev))).verify(jws)
    assert txn.transaction_id == "xc"


# ============================ payload/JWS не логируются ============================


def test_payload_and_jws_not_logged_on_success(tmp_path, caplog):
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    marker_tid = "MARKER_TXN_9988776655"  # noqa: S105
    payload = sk.transaction_payload(
        transaction_id=marker_tid, product_id="p", bundle_id="secret.bundle.id"
    )
    jws = sk.sign_jws(payload, leaf, [leaf, root])

    with caplog.at_level("DEBUG"):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)

    # Ни сам JWS, ни тело (bundleId) не должны попасть в лог верификатора.
    text = caplog.text
    assert jws not in text
    assert "secret.bundle.id" not in text


def test_payload_and_jws_not_logged_on_failure(tmp_path, caplog):
    """При отказе верификации крипто-детали/JWS не логируются (05-security)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    jws = sk.build_transaction_jws(
        tmp_path / "c", transaction_id="failtid", product_id="p", trust_root=False
    )
    with caplog.at_level("DEBUG"), pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(empty))).verify(jws)
    assert jws not in caplog.text


# ============ ВРЕМЕННЫЙ тестовый обход верификации (ADR-043) ============


def test_skip_verify_accepts_transaction_not_anchored_to_trusted_root(tmp_path):
    """STOREKIT_INSECURE_SKIP_VERIFY=true → цепочка/подпись не проверяются, payload читается."""
    foreign_root = sk.make_root()
    leaf = sk.make_leaf(foreign_root)
    # В каталоге доверенных roots — ЧУЖОЙ root: при обычном режиме это отказ.
    sk.write_roots(tmp_path, [sk.make_root()])
    payload = sk.transaction_payload(transaction_id="t_skip_1", product_id="week_6.99_not_trial")
    jws = sk.sign_jws(payload, leaf, [leaf, foreign_root])

    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(jws)

    txn = StoreKitVerifier(_settings(str(tmp_path), skip_verify=True)).verify(jws)
    assert txn.transaction_id == "t_skip_1"
    assert txn.product_id == "week_6.99_not_trial"


def test_skip_verify_accepts_tampered_signature(tmp_path):
    """Подделанный payload (подпись невалидна) при обходе принимается — это цена режима."""
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(tmp_path, [root])
    jws = sk.sign_jws(
        sk.transaction_payload(transaction_id="t_skip_2", product_id="week_6.99_not_trial"),
        leaf,
        [leaf, root],
    )
    header, _payload_b64, signature = jws.split(".")
    forged = json.dumps(
        {**sk.transaction_payload(transaction_id="t_forged", product_id="week_6.99_not_trial")}
    ).encode()
    tampered = ".".join([header, base64.urlsafe_b64encode(forged).decode().rstrip("="), signature])

    with pytest.raises(StoreKitVerificationError):
        StoreKitVerifier(_settings(str(tmp_path))).verify(tampered)

    txn = StoreKitVerifier(_settings(str(tmp_path), skip_verify=True)).verify(tampered)
    assert txn.transaction_id == "t_forged"


def test_skip_verify_still_rejects_non_jws_and_broken_payload(tmp_path):
    """Обход снимает только крипто-проверку: структурные отказы остаются."""
    verifier = StoreKitVerifier(_settings(str(tmp_path), skip_verify=True))

    with pytest.raises(StoreKitVerificationError):
        verifier.verify("not-a-jws")

    root = sk.make_root()
    leaf = sk.make_leaf(root)
    # payload без transactionId → отказ даже при обходе.
    jws = sk.sign_jws({"bundleId": "mba.gipsy.lovable"}, leaf, [leaf, root])
    with pytest.raises(StoreKitVerificationError):
        verifier.verify(jws)


def test_skip_verify_does_not_log_payload_or_jws(tmp_path, caplog):
    """В режиме обхода в логи идёт только предупреждение, без payload/JWS (docs/05-security)."""
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    jws = sk.sign_jws(
        sk.transaction_payload(transaction_id="t_skip_3", product_id="week_6.99_not_trial"),
        leaf,
        [leaf, root],
    )

    with caplog.at_level("WARNING"):
        StoreKitVerifier(_settings(str(tmp_path), skip_verify=True)).verify(jws)

    text = " ".join(record.getMessage() for record in caplog.records)
    assert "storekit_verification_bypassed" in text
    assert jws not in text
    assert "t_skip_3" not in text
