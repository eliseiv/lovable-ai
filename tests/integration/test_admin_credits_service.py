"""Integration: admin_service.grant_credits + usage._try_decrement_credit (ADR-021 §D, §10).

Источник истины — docs/modules/admin/03-architecture.md §3 + docs/modules/billing/03 §10.
Проверяет relative-UPDATE семантику грантов (баланс += amount от АКТУАЛЬНОГО значения строки,
не от прочитанного снапшота) — конкурентно-безопасное начисление поверх списания.
Реальный Postgres (одна тест-сессия с savepoint-откатом).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.errors import ProblemException
from app.billing.usage import _try_decrement_credit
from app.db.models import CreditGrant, User
from app.services import admin_service

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str, *, balance: int = 0) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
        bonus_generations_balance=balance,
    )
    session.add(user)
    await session.flush()
    return user


async def test_grant_uses_relative_update_not_snapshot(session):
    """grant(+N) применяет дельту к АКТУАЛЬНОМУ значению строки, а не к прочитанному снапшоту.

    Сценарий гонки: между чтением баланса и записью грант-апдейта успел пройти декремент
    списания (balance -= 1). Relative-UPDATE (SET balance = balance + :amount) учитывает это
    списание → итог = start + N - 1. Абсолютная запись (snapshot) перезатёрла бы списание
    (over-credit, итог = start + N).
    """
    user = await _user(session, "u_cred_rel00000001", balance=5)

    # Симулируем «между read и write грант-логики прошёл декремент со старта генерации».
    # _try_decrement_credit использует тот же относительный UPDATE-стиль.
    assert await _try_decrement_credit(session, user.id) is True  # balance: 5 → 4
    await session.flush()

    # Грант +10 от АКТУАЛЬНОГО (4), а не от снапшота 5.
    result = await admin_service.grant_credits(
        session, user_id=user.id, amount=10, reason=None, idempotency_key=None
    )
    assert result.bonus_generations_balance == 14  # 4 + 10, НЕ 5 + 10 = 15

    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 14


async def test_concurrent_grant_and_decrement_net_effect(session):
    """grant(+N) и _try_decrement_credit(-1) на одной строке → итог = start + N - 1.

    Оба используют относительный UPDATE → СУБД применяет каждую дельту к актуальному значению
    строки (не теряет конкурентную мутацию). Последовательная эмуляция гонки в одной сессии
    воспроизводит инвариант net-effect (полноценный параллелизм требует раздельных коннектов;
    relative-UPDATE-семантика проверяется детерминированно).
    """
    start, n = 8, 5
    user = await _user(session, "u_cred_net00000001", balance=start)

    await admin_service.grant_credits(
        session, user_id=user.id, amount=n, reason=None, idempotency_key=None
    )
    assert await _try_decrement_credit(session, user.id) is True
    await session.flush()

    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == start + n - 1  # 12


async def test_grant_negative_below_zero_raises_409_and_no_ledger_row(autonomous_db):
    """amount<0, уводящий < 0 → ProblemException(409), строка ledger НЕ пишется, баланс цел.

    grant_credits на 409-ветке делает РЕАЛЬНЫЙ session.rollback(). Проверяем на автономной
    сессии (session_scope, отдельные транзакции — не savepoint), иначе сервисный rollback
    развернул бы тест-сид. Юзер сидируется/чистится в собственных транзакциях.
    """
    from app.db.session import session_scope

    uid = "u_cred_neg00000001"
    try:
        async with session_scope() as s:
            await _user(s, uid, balance=2)
            await s.commit()

        async with session_scope() as s:
            with pytest.raises(ProblemException) as exc:
                await admin_service.grant_credits(
                    s, user_id=uid, amount=-5, reason=None, idempotency_key=None
                )
            assert exc.value.status == 409

        async with session_scope() as s:
            balance = await s.scalar(select(User.bonus_generations_balance).where(User.id == uid))
            assert balance == 2  # не изменён
            count = await s.scalar(
                select(func.count()).select_from(CreditGrant).where(CreditGrant.user_id == uid)
            )
            assert count == 0  # строка credit_grants НЕ записана (rollback)
    finally:
        async with session_scope() as s:
            await s.execute(CreditGrant.__table__.delete().where(CreditGrant.user_id == uid))
            await s.execute(User.__table__.delete().where(User.id == uid))
            await s.commit()


async def test_idempotency_key_replay_returns_current_balance_no_double(session):
    """Повтор grant с тем же Idempotency-Key → no-op (одна строка, баланс не удваивается)."""
    user = await _user(session, "u_cred_idem0000001", balance=0)
    r1 = await admin_service.grant_credits(
        session, user_id=user.id, amount=6, reason=None, idempotency_key="k1"
    )
    r2 = await admin_service.grant_credits(
        session, user_id=user.id, amount=6, reason=None, idempotency_key="k1"
    )
    assert r1.bonus_generations_balance == 6
    assert r2.bonus_generations_balance == 6

    count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.user_id == user.id)
    )
    assert count == 1


async def test_amount_zero_raises_422(session):
    """amount==0 → ProblemException(422)."""
    user = await _user(session, "u_cred_zero0000001")
    with pytest.raises(ProblemException) as exc:
        await admin_service.grant_credits(
            session, user_id=user.id, amount=0, reason=None, idempotency_key=None
        )
    assert exc.value.status == 422


async def test_grant_unknown_user_raises_404(session):
    """Нет user_id → ProblemException(404)."""
    with pytest.raises(ProblemException) as exc:
        await admin_service.grant_credits(
            session, user_id="u_cred_nosuch001", amount=5, reason=None, idempotency_key=None
        )
    assert exc.value.status == 404


async def test_try_decrement_credit_at_zero_returns_false_and_floor(session):
    """_try_decrement_credit при balance==0 → False (списания нет, инвариант >= 0)."""
    user = await _user(session, "u_cred_dec000001", balance=0)
    assert await _try_decrement_credit(session, user.id) is False
    fresh = await session.get(User, user.id)
    await session.refresh(fresh)
    assert fresh.bonus_generations_balance == 0
