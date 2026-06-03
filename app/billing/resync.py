"""getProfile-ресинк подписок (dual-source fallback, ADR-009, docs/billing/03 §3).

- Периодический (Celery-beat billing.resync, BILLING_RESYNC_INTERVAL_S): сверка
  subscriptions для пользователей с протухшим synced_at или status ∈ {grace, billing_issue}.
- Lazy (по требованию на гейте / billing/me): best-effort getProfile при протухшем кэше;
  fail-open на кэш при недоступности Adapty.

Ресинк НЕ перетирает более свежее вебхук-состояние: профиль пишется только если
subscriptions.synced_at старше TTL (вебхук обновляет synced_at=now, значит свежий
вебхук имеет приоритет). Rate-limit к Adapty — в adapty_client (token-bucket).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import subscription_state
from app.billing.adapty_client import (
    AdaptyClient,
    AdaptyError,
    AdaptyTransientError,
    get_adapty_client,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Subscription
from app.db.session import session_scope
from app.observability import metrics
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


def _is_stale(synced_at: datetime, ttl_s: int, now: datetime) -> bool:
    return synced_at < now - timedelta(seconds=ttl_s)


async def resync_user(
    session: AsyncSession,
    *,
    user_id: str,
    sub: Subscription,
    client: AdaptyClient,
) -> bool:
    """Ресинк одной подписки через getProfile. True, если применён апдейт.

    Идемпотентно: повторный ресинк того же профиля даёт тот же результат. Коммит — на
    стороне вызывающего. Транзиентная ошибка Adapty пробрасывается (backoff-ретрай).
    """
    profile = await client.get_profile(user_id)
    if profile is None:
        # Профиль не найден в Adapty (нет покупок) — оставляем кэш как есть, только
        # отмечаем свежесть, чтобы не дёргать Adapty повторно каждый тик.
        sub.synced_at = datetime.now(UTC)
        return False
    subscription_state.apply_profile_resync(sub, profile)
    return True


async def run_periodic_resync(session: AsyncSession, client: AdaptyClient) -> int:
    """Beat-ресинк: подписки с протухшим synced_at ИЛИ в grace/billing_issue (docs §3.1).

    Возвращает число успешно ресинкнутых. Транзиентные ошибки Adapty по конкретному
    пользователю логируются и не валят весь батч (per-user изоляция).

    Sprint 6 (TD-009, ADR-016): батч + курсор — `.limit(BILLING_RESYNC_BATCH_SIZE)` +
    `ORDER BY synced_at ASC` (самые протухшие первыми; хвост — на последующих тиках).
    Метрики: lovable_billing_resync_batch{result=full|partial} (full = батч заполнен →
    есть хвост), lovable_adapty_resync_lag_seconds (возраст самой протухшей до ресинка).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=settings.billing_resync_interval_s)
    batch_size = settings.billing_resync_batch_size

    result = await session.execute(
        select(Subscription)
        .where(
            or_(
                Subscription.synced_at < stale_cutoff,
                Subscription.status.in_(
                    (
                        subscription_state.STATUS_GRACE,
                        subscription_state.STATUS_BILLING_ISSUE,
                    )
                ),
            )
        )
        # Курсор: самые протухшие (минимальный synced_at) — первыми (TD-009).
        .order_by(Subscription.synced_at.asc())
        .limit(batch_size)
    )
    subs = list(result.scalars().all())

    # Resync-lag: возраст самой протухшей подписки в батче (до обработки) — отставание ресинка.
    if subs:
        oldest = min(_aware(sub.synced_at) for sub in subs)
        metrics.adapty_resync_lag_seconds.set((now - oldest).total_seconds())
    else:
        metrics.adapty_resync_lag_seconds.set(0.0)

    resynced = 0
    for sub in subs:
        try:
            if await resync_user(session, user_id=sub.user_id, sub=sub, client=client):
                resynced += 1
        except AdaptyTransientError as exc:
            # Транзиентно — не валим батч; добьётся следующим тиком/ретраем.
            logger.warning(
                "billing_resync_transient", extra={"user_id": sub.user_id, "error": str(exc)}
            )
        except AdaptyError as exc:
            logger.warning(
                "billing_resync_error", extra={"user_id": sub.user_id, "error": str(exc)}
            )
    await session.commit()
    # full = батч заполнен под лимит (вероятен хвост на следующих тиках); partial = всё влезло.
    batch_result = "full" if len(subs) >= batch_size else "partial"
    metrics.billing_resync_batch.labels(result=batch_result).observe(len(subs))
    if resynced:
        logger.info("billing_resync_done", extra={"count": resynced})
    return resynced


def _aware(dt: datetime) -> datetime:
    """Приводит naive-datetime (из БД без tz) к UTC-aware для сравнения возраста."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def lazy_resync_if_stale(session: AsyncSession, user_id: str) -> Subscription | None:
    """Lazy-ресинк на гейте/billing/me при протухшем synced_at (docs §3.2).

    Best-effort: при недоступности Adapty — fail-open на кэш (читаем subscriptions как
    есть, не блокируем пользователя). Создаёт строку, если её нет и Adapty вернул профиль.
    Возвращает актуальную (возможно обновлённую) строку subscriptions или None (нет подписки).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    sub = await subscription_state.get_subscription(session, user_id)
    if sub is None:
        # Нет строки → free-дефолт. Lazy-ресинк не создаёт строку на горячем пути для
        # пользователей без подписки (избегаем лишних вызовов Adapty); периодический
        # ресинк/вебхук создаст её при первой покупке.
        return None
    if not _is_stale(sub.synced_at, settings.billing_resync_interval_s, now):
        return sub

    client = get_adapty_client()
    try:
        applied = await resync_user(session, user_id=user_id, sub=sub, client=client)
    except AdaptyError as exc:
        # Fail-open на кэш: недоступность Adapty не блокирует гейт (docs §3.2).
        logger.warning(
            "billing_lazy_resync_failopen", extra={"user_id": user_id, "error": str(exc)}
        )
        return sub
    if applied:
        await session.commit()
    return sub


async def _run_periodic_resync_scope() -> int:
    client = get_adapty_client()
    async with session_scope() as session:
        return await run_periodic_resync(session, client)


@celery_app.task(name="billing.resync", queue="llm")
def resync_task() -> int:
    """Celery-beat: периодический getProfile-ресинк подписок (fallback на пропущенные вебхуки)."""
    return asyncio.run(_run_periodic_resync_scope())
