"""Integration: прямой StoreKit-путь покупок (ADR-039 §B/§C/§D, docs/06 §Contract).

Реальный Postgres (client шарит тест-сессию). Верификатор-синглтон переинициализируется на
тест-каталог доверенных roots (фикстура storekit_env: shared тест-root+leaf, TOKEN_PACK_PRODUCTS
на cached Settings). Начисление — на Bearer-user_id (seeded_user), НЕ на payload.

Покрытие (docs/06 §Contract a–f + follow_up_for_qa):
  (a) tokens/purchase applied → balance += N, credit_grants(created_by='storekit',
      idempotency_key='storekit:'+tid), store_transactions(kind='tokens_purchase') — одна txn;
  (b) глобальная идемпотентность: повтор tid (тем же / ДРУГИМ user) → duplicate, без повторного
      начисления (replay-защита кросс-аккаунт);
  (c) unknown product → ignored:unknown_token_product; revoked → ignored:revoked;
  (d) subscription/sync applied → subscriptions pro/active/store='storekit'/expires_at, токены НЕ
      начислены; expired → ignored:expired; повтор → duplicate; renewal → обновление expires_at;
  (e) auth/fail-closed: нет/невалидный Bearer → 401; roots не сконфигурированы / битый JWS → 422;
  (h) payload/JWS не логируются (caplog).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import CreditGrant, StoreTransaction, Subscription, User
from tests.support import storekit_jws as sk

pytestmark = pytest.mark.asyncio

CANONICAL_CSV = (
    "100_tokens_9.99:100,250_tokens_19.99:250,500_tokens_34.99:500,"
    "1000_tokens_59.99:1000,2000_tokens_99.99:2000"
)

# seeded_user из conftest — Bearer-владелец начисления.
BEARER_UID = "u_testowner000000000000"


def _future_ms(days: int = 365) -> int:
    return int((datetime.now(UTC) + timedelta(days=days)).timestamp() * 1000)


def _past_ms(days: int = 5) -> int:
    return int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)


@pytest.fixture
def storekit_env(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """Переинициализирует StoreKit-верификатор на shared тест-root; ставит TOKEN_PACK_PRODUCTS.

    Возвращает объект с make_jws(**payload) — все транзакции теста подписаны ОДНИМ тест-leaf под
    ОДНИМ доверенным root (синглтон грузит roots один раз). appstore_bundle_id пуст → bundle-check
    пропущен (тест). Сброс синглтона до и после — изоляция от других тестов/каталогов.
    """
    import app.billing.storekit as sk_mod

    settings = get_settings()
    cert_dir = tmp_path / "appstore"
    root = sk.make_root()
    leaf = sk.make_leaf(root)
    sk.write_roots(cert_dir, [root])
    monkeypatch.setattr(settings, "appstore_root_cert_dir", str(cert_dir), raising=False)
    monkeypatch.setattr(settings, "appstore_bundle_id", "", raising=False)
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    sk_mod._verifier_singleton = None

    def make_jws(**payload) -> str:  # noqa: ANN003
        return sk.sign_jws(sk.transaction_payload(**payload), leaf, [leaf, root])

    try:
        yield SimpleNamespace(cert_dir=cert_dir, make_jws=make_jws)
    finally:
        sk_mod._verifier_singleton = None


async def _balance(session, uid: str) -> int:  # noqa: ANN001
    return await session.scalar(select(User.bonus_generations_balance).where(User.id == uid))


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ============================ (a) tokens/purchase applied ============================


async def test_tokens_purchase_applied_credits_bearer_user(
    client, session, seeded_user, storekit_env, auth_headers
):
    jws = storekit_env.make_jws(transaction_id="tx_tok_1", product_id="250_tokens_19.99")
    resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied", "tokens_granted": 250}

    # Начислено на Bearer-user (seeded_user), НЕ на payload-account.
    assert await _balance(session, BEARER_UID) == 250

    grant = (
        await session.execute(
            select(CreditGrant).where(CreditGrant.idempotency_key == "storekit:tx_tok_1")
        )
    ).scalar_one()
    assert grant.created_by == "storekit"
    assert grant.reason == "storekit:tokens_purchase"
    assert grant.amount == 250
    assert grant.user_id == BEARER_UID

    st = (
        await session.execute(
            select(StoreTransaction).where(StoreTransaction.transaction_id == "tx_tok_1")
        )
    ).scalar_one()
    assert st.kind == "tokens_purchase"
    assert st.user_id == BEARER_UID
    assert st.amount == 250
    assert st.product_id == "250_tokens_19.99"


async def test_tokens_purchase_credits_caller_not_payload_account(
    client, session, seeded_user, storekit_env, auth_headers
):
    """JWS доказывает покупку; получатель — Bearer-вызывающий (payload не несёт наш user_id)."""
    jws = storekit_env.make_jws(transaction_id="tx_caller", product_id="100_tokens_9.99")
    resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
    assert resp.json()["status"] == "applied"
    st = (
        await session.execute(
            select(StoreTransaction).where(StoreTransaction.transaction_id == "tx_caller")
        )
    ).scalar_one()
    assert st.user_id == BEARER_UID


# NB: глобальная идемпотентность / кросс-аккаунт-replay / subscription-duplicate вынесены в
# tests/integration/test_storekit_idempotency.py — путь дубликата в коде выполняет
# session.rollback() (корректное прод-поведение, ADR-039 §D: atomic insert-and-catch, отдельная
# транзакция на запрос). Shared-session client-харнесс (один session на оба запроса) не переживает
# такой rollback — идемпотентность тестируется на РЕАЛЬНЫХ раздельных транзакциях (session_scope).


# ============================ (c) unknown / revoked ============================


async def test_tokens_purchase_unknown_product_ignored(
    client, session, seeded_user, storekit_env, auth_headers
):
    jws = storekit_env.make_jws(transaction_id="tx_unk", product_id="9999_not_a_pack")
    resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}
    assert await _balance(session, BEARER_UID) == 0
    # store_transactions НЕ создаётся для неизвестного SKU.
    st_count = await session.scalar(
        select(func.count())
        .select_from(StoreTransaction)
        .where(StoreTransaction.transaction_id == "tx_unk")
    )
    assert st_count == 0


async def test_tokens_purchase_revoked_ignored(
    client, session, seeded_user, storekit_env, auth_headers
):
    jws = storekit_env.make_jws(
        transaction_id="tx_rev", product_id="250_tokens_19.99", revoked=True
    )
    resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "revoked"}
    assert await _balance(session, BEARER_UID) == 0


# ============================ (d) subscription/sync ============================


async def test_subscription_sync_applied_sets_pro_no_tokens(
    client, session, seeded_user, storekit_env, auth_headers
):
    jws = storekit_env.make_jws(
        transaction_id="sub_1",
        product_id="pro_yearly",
        environment="Sandbox",
        expires_ms=_future_ms(),
    )
    resp = await client.post("/v1/subscription/sync", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["access_level"] == "pro"
    assert "expires_at" in body

    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == BEARER_UID))
    ).scalar_one()
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert sub.store == "storekit"
    assert sub.expires_at is not None

    # Подписка токены НЕ начисляет.
    assert await _balance(session, BEARER_UID) == 0
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.user_id == BEARER_UID)
    )
    assert grant_count == 0
    # store_transactions(kind='subscription_sync') создана.
    st = (
        await session.execute(
            select(StoreTransaction).where(StoreTransaction.transaction_id == "sub_1")
        )
    ).scalar_one()
    assert st.kind == "subscription_sync"
    assert st.amount is None


async def test_subscription_sync_expired_ignored(
    client, session, seeded_user, storekit_env, auth_headers
):
    jws = storekit_env.make_jws(
        transaction_id="sub_exp", product_id="pro_yearly", expires_ms=_past_ms()
    )
    resp = await client.post("/v1/subscription/sync", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "expired"}
    # Подписка НЕ создана.
    sub_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == BEARER_UID)
    )
    assert sub_count == 0


async def test_subscription_sync_revoked_ignored(
    client, session, seeded_user, storekit_env, auth_headers
):
    jws = storekit_env.make_jws(
        transaction_id="sub_rev",
        product_id="pro_yearly",
        expires_ms=_future_ms(),
        revoked=True,
    )
    resp = await client.post("/v1/subscription/sync", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "revoked"}


async def test_subscription_sync_renewal_updates_expires_at(
    client, session, seeded_user, storekit_env, auth_headers
):
    """Renewal = новая transaction_id (тот же original) → новая строка → обновление expires_at."""
    jws1 = storekit_env.make_jws(
        transaction_id="sub_r1",
        original_transaction_id="orig_sub",
        product_id="pro_yearly",
        expires_ms=_future_ms(30),
    )
    r1 = await client.post("/v1/subscription/sync", json={"jws": jws1}, headers=auth_headers)
    assert r1.json()["status"] == "applied"
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == BEARER_UID))
    ).scalar_one()
    first_expires = sub.expires_at

    jws2 = storekit_env.make_jws(
        transaction_id="sub_r2",
        original_transaction_id="orig_sub",
        product_id="pro_yearly",
        expires_ms=_future_ms(400),
    )
    r2 = await client.post("/v1/subscription/sync", json={"jws": jws2}, headers=auth_headers)
    assert r2.json()["status"] == "applied"
    await session.refresh(sub)
    assert sub.expires_at > first_expires
    # Две store_transactions-строки (renewal-цепочка).
    st_count = await session.scalar(
        select(func.count())
        .select_from(StoreTransaction)
        .where(StoreTransaction.original_transaction_id == "orig_sub")
    )
    assert st_count == 2


# ============================ (e) auth / fail-closed ============================


async def test_tokens_purchase_no_bearer_401(client, storekit_env):
    resp = await client.post("/v1/tokens/purchase", json={"jws": "x.y.z"})
    assert resp.status_code == 401


async def test_tokens_purchase_invalid_bearer_401(client, storekit_env):
    resp = await client.post(
        "/v1/tokens/purchase", json={"jws": "x.y.z"}, headers=_auth("wrong-key")
    )
    assert resp.status_code == 401


async def test_subscription_sync_no_bearer_401(client, storekit_env):
    resp = await client.post("/v1/subscription/sync", json={"jws": "x.y.z"})
    assert resp.status_code == 401


async def test_invalid_jws_422_fail_closed(
    client, session, seeded_user, storekit_env, auth_headers
):
    """Мусорный JWS → 422 invalid-storekit-transaction, крипто-детали не раскрыты."""
    resp = await client.post(
        "/v1/tokens/purchase", json={"jws": "garbage-not-a-jws"}, headers=auth_headers
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("invalid-storekit-transaction")
    # Крипто-детали (root/chain/signature/x5c) не в теле.
    blob = str(body).lower()
    for leak in ("x5c", "chain", "signature", "root cert", "es256"):
        assert leak not in blob


async def test_fail_closed_when_roots_not_configured_422(
    client, session, seeded_user, auth_headers, tmp_path, monkeypatch
):
    """APPSTORE_ROOT_CERT_DIR пуст (нет сертификатов) → 422 fail-closed, без начисления."""
    import app.billing.storekit as sk_mod

    settings = get_settings()
    empty_dir = tmp_path / "empty_roots"
    empty_dir.mkdir()
    monkeypatch.setattr(settings, "appstore_root_cert_dir", str(empty_dir), raising=False)
    monkeypatch.setattr(settings, "appstore_bundle_id", "", raising=False)
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    sk_mod._verifier_singleton = None
    try:
        # Валидно-подписанный JWS, но доверенных roots нет → отказ.
        cert_dir = tmp_path / "certs"
        jws = sk.build_transaction_jws(
            cert_dir,
            transaction_id="tx_noroots",
            product_id="250_tokens_19.99",
            trust_root=False,
        )
        resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
        assert resp.status_code == 422
        assert resp.json()["type"].endswith("invalid-storekit-transaction")
        # Начисление НЕ произошло.
        assert await _balance(session, BEARER_UID) == 0
        st_count = await session.scalar(
            select(func.count())
            .select_from(StoreTransaction)
            .where(StoreTransaction.transaction_id == "tx_noroots")
        )
        assert st_count == 0
    finally:
        sk_mod._verifier_singleton = None


async def test_invalid_chain_link_422_not_500_no_grant(
    client, session, seeded_user, auth_headers, tmp_path, monkeypatch
):
    """Невалидное ЗВЕНО цепочки (leaf не подписан trusted root) → 422 fail-closed, НЕ 500.

    x5c=[attacker_leaf, trusted_root]: цепочка присутствует, root доверен (в trusted dir), но
    звено leaf→root подписью не проходит (leaf подписан ЧУЖИМ attacker_root). Верификатор ловит
    InvalidSignature в последовательном цикле → StoreKitVerificationError → роутер отдаёт 422
    invalid-storekit-transaction (крипто-детали не раскрыты), НЕ 500. Оба клиентских эндпоинта;
    начисление токенов и подписка НЕ создаются. Закрывает пробел review (последовательный цикл
    _verify_signed_by, ДО фикса ADR-039 давал 500 на этом входе).
    """
    import app.billing.storekit as sk_mod

    settings = get_settings()
    cert_dir = tmp_path / "appstore"
    trusted_root = sk.make_root("Trusted Root")
    attacker_root = sk.make_root("Attacker Root")
    attacker_leaf = sk.make_leaf(attacker_root)  # подписан attacker_root, НЕ trusted_root
    sk.write_roots(cert_dir, [trusted_root])  # доверяем trusted_root → anchor прошёл бы
    monkeypatch.setattr(settings, "appstore_root_cert_dir", str(cert_dir), raising=False)
    monkeypatch.setattr(settings, "appstore_bundle_id", "", raising=False)
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    sk_mod._verifier_singleton = None
    try:
        payload = sk.transaction_payload(
            transaction_id="tx_badlink",
            product_id="250_tokens_19.99",
            expires_ms=_future_ms(),
        )
        jws = sk.sign_jws(payload, attacker_leaf, [attacker_leaf, trusted_root])
        for path in ("/v1/tokens/purchase", "/v1/subscription/sync"):
            resp = await client.post(path, json={"jws": jws}, headers=auth_headers)
            assert resp.status_code == 422, (path, resp.status_code, resp.text)
            body = resp.json()
            assert body["type"].endswith("invalid-storekit-transaction")
            # Крипто-детали (chain/signature/root/x5c) не раскрыты в теле ответа.
            blob = str(body).lower()
            for leak in ("x5c", "chain", "signature", "root cert", "es256"):
                assert leak not in blob
        # Начисление НЕ произошло и подписка НЕ создана.
        assert await _balance(session, BEARER_UID) == 0
        st_count = await session.scalar(
            select(func.count())
            .select_from(StoreTransaction)
            .where(StoreTransaction.transaction_id == "tx_badlink")
        )
        assert st_count == 0
        sub_count = await session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.user_id == BEARER_UID)
        )
        assert sub_count == 0
    finally:
        sk_mod._verifier_singleton = None


async def test_bundle_mismatch_422(
    client, session, seeded_user, storekit_env, auth_headers, monkeypatch
):
    """Непустой APPSTORE_BUNDLE_ID + mismatch в payload → 422."""
    import app.billing.storekit as sk_mod

    settings = get_settings()
    monkeypatch.setattr(settings, "appstore_bundle_id", "mba.gipsy.lovable", raising=False)
    sk_mod._verifier_singleton = None  # перечитать bundle_id
    jws = storekit_env.make_jws(
        transaction_id="tx_bundle", product_id="250_tokens_19.99", bundle_id="com.evil.app"
    )
    resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
    assert resp.status_code == 422


# ============================ (h) payload/JWS не логируются ============================


async def test_payload_jws_not_logged(
    client, session, seeded_user, storekit_env, auth_headers, caplog
):
    jws = storekit_env.make_jws(
        transaction_id="tx_log", product_id="250_tokens_19.99", bundle_id="secret.bundle.xyz"
    )
    with caplog.at_level("DEBUG"):
        resp = await client.post("/v1/tokens/purchase", json={"jws": jws}, headers=auth_headers)
    assert resp.json()["status"] == "applied"
    # Ни JWS, ни секретный bundleId не должны попасть в логи; tid допустим.
    assert jws not in caplog.text
    assert "secret.bundle.xyz" not in caplog.text
