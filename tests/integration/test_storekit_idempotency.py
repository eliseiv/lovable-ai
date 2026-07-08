"""Integration: глобальная идемпотентность прямого StoreKit-пути (ADR-039 §D) — РЕАЛЬНЫЕ транзакции.

Путь дубликата в apply_tokens_purchase/apply_subscription_sync выполняет session.commit() и, при
конфликте PK store_transactions.transaction_id, session.rollback() (корректное прод-поведение:
atomic insert-and-catch, ОТДЕЛЬНАЯ транзакция на каждый запрос). Shared-session client-харнесс не
переживает rollback второго запроса (один session на оба), поэтому идемпотентность проверяется на
ГЕНУИННО РАЗДЕЛЬНЫХ транзакциях через session_scope() (каждый apply_* коммитит в реальную БД).

Данные коммитятся в тест-БД → фикстура real_users чистит строки по известным UID до и после теста.

Покрытие (docs/06 §Contract b + follow_up_for_qa §5):
  - повтор той же transaction_id ТЕМ ЖЕ user → duplicate, баланс не растёт повторно;
  - повтор ДРУГИМ user (кросс-аккаунтная переигровка чужого JWS) → duplicate, второму НЕ начислено,
    store_transactions по-прежнему на первого user (глобальный PK-дедуп);
  - subscription_sync duplicate: повтор той же tid → duplicate, одна строка store_transactions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.billing.storekit import VerifiedTransaction
from app.billing.storekit_purchase import apply_subscription_sync, apply_tokens_purchase
from app.core.config import get_settings
from app.db.models import CreditGrant, StoreTransaction, Subscription, User

pytestmark = pytest.mark.asyncio

CANONICAL_CSV = (
    "100_tokens_9.99:100,250_tokens_19.99:250,500_tokens_34.99:500,"
    "1000_tokens_59.99:1000,2000_tokens_99.99:2000"
)

UID_A = "u_skidem_a00000000001"
UID_B = "u_skidem_b00000000001"


def _tokens_txn(transaction_id: str, product_id: str = "250_tokens_19.99") -> VerifiedTransaction:
    return VerifiedTransaction(
        transaction_id=transaction_id,
        original_transaction_id=transaction_id,
        product_id=product_id,
        expires_at=None,
        revoked=False,
        environment="Xcode",
    )


def _sub_txn(transaction_id: str) -> VerifiedTransaction:
    from datetime import UTC, datetime, timedelta

    return VerifiedTransaction(
        transaction_id=transaction_id,
        original_transaction_id=transaction_id,
        product_id="pro_yearly",
        expires_at=datetime.now(UTC) + timedelta(days=365),
        revoked=False,
        environment="Sandbox",
    )


@pytest_asyncio.fixture
async def real_users(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    """Два реально закоммиченных user (FK) + TOKEN_PACK_PRODUCTS; cleanup строк до/после.

    autonomous_db сбрасывает кэш глобального движка → session_scope биндится к движку текущего
    loop теста (иначе asyncpg «Event loop is closed» на Windows).
    """
    from app.db.session import session_scope

    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    uids = [UID_A, UID_B]

    async def _cleanup() -> None:
        async with session_scope() as s:
            await s.execute(delete(StoreTransaction).where(StoreTransaction.user_id.in_(uids)))
            await s.execute(delete(CreditGrant).where(CreditGrant.user_id.in_(uids)))
            await s.execute(delete(Subscription).where(Subscription.user_id.in_(uids)))
            await s.execute(delete(User).where(User.id.in_(uids)))
            await s.commit()

    await _cleanup()
    async with session_scope() as s:
        for uid in uids:
            s.add(
                User(
                    id=uid,
                    api_key_hash=None,
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
        await s.commit()
    try:
        yield uids
    finally:
        await _cleanup()


async def _balance(uid: str) -> int:
    from app.db.session import session_scope

    async with session_scope() as s:
        return await s.scalar(select(User.bonus_generations_balance).where(User.id == uid))


async def test_tokens_purchase_global_idempotency_same_user(real_users):
    from app.db.session import session_scope

    txn = _tokens_txn("skidem_tx_same", "500_tokens_34.99")
    async with session_scope() as s1:
        r1 = await apply_tokens_purchase(s1, user_id=UID_A, txn=txn)
    assert r1.status == "applied"
    assert r1.tokens_granted == 500
    assert await _balance(UID_A) == 500

    # Повтор той же transaction_id → duplicate, без повторного начисления.
    async with session_scope() as s2:
        r2 = await apply_tokens_purchase(s2, user_id=UID_A, txn=txn)
    assert r2.status == "duplicate"
    assert await _balance(UID_A) == 500

    async with session_scope() as s3:
        st_count = await s3.scalar(
            select(func.count())
            .select_from(StoreTransaction)
            .where(StoreTransaction.transaction_id == "skidem_tx_same")
        )
        grant_count = await s3.scalar(
            select(func.count())
            .select_from(CreditGrant)
            .where(CreditGrant.idempotency_key == "storekit:skidem_tx_same")
        )
    assert st_count == 1
    assert grant_count == 1


async def test_tokens_purchase_cross_account_replay_blocked(real_users):
    """Тот же tid, переигранный ДРУГИМ user → duplicate; второму НЕ начислено (глобальный PK)."""
    from app.db.session import session_scope

    txn = _tokens_txn("skidem_tx_replay", "1000_tokens_59.99")
    async with session_scope() as s1:
        r1 = await apply_tokens_purchase(s1, user_id=UID_A, txn=txn)
    assert r1.status == "applied"
    assert await _balance(UID_A) == 1000

    # Переигровка тем же tid, но user B.
    async with session_scope() as s2:
        r2 = await apply_tokens_purchase(s2, user_id=UID_B, txn=txn)
    assert r2.status == "duplicate"
    # Второму НЕ начислено.
    assert await _balance(UID_B) == 0
    # store_transactions по-прежнему принадлежит первому user.
    async with session_scope() as s3:
        st = (
            await s3.execute(
                select(StoreTransaction).where(
                    StoreTransaction.transaction_id == "skidem_tx_replay"
                )
            )
        ).scalar_one()
    assert st.user_id == UID_A


async def test_subscription_sync_duplicate(real_users):
    from app.db.session import session_scope

    txn = _sub_txn("skidem_sub_dup")
    async with session_scope() as s1:
        r1 = await apply_subscription_sync(s1, user_id=UID_A, txn=txn)
    assert r1.status == "applied"
    assert r1.access_level == "pro"

    async with session_scope() as s2:
        r2 = await apply_subscription_sync(s2, user_id=UID_A, txn=txn)
    assert r2.status == "duplicate"

    async with session_scope() as s3:
        st_count = await s3.scalar(
            select(func.count())
            .select_from(StoreTransaction)
            .where(StoreTransaction.transaction_id == "skidem_sub_dup")
        )
    assert st_count == 1
