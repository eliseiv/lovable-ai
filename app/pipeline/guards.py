"""Четыре гарда fix-loop от бесконечного цикла и runaway-затрат (docs §C).

Проверяются ПЕРЕД каждым витком — на входе в FIXING, до постановки task_fix:
  (a) max_fix_attempts: retry_count vs max_fix_attempts → build_unrecoverable;
  (b) job_budget_usd: spend_usd vs budget_usd → budget_exhausted;
  (c) wall_clock_deadline: now() vs wall_clock_deadline → wall_clock_exceeded;
  (d) no-progress: failure_signature == last_failure_signature → no_progress.

Гард (d) — ЕДИНСТВЕННАЯ точка записи last_failure_signature (docs §C(d), ADR-005):
на входе в FIXING сигнатура текущего фейла сравнивается с предыдущей и затем
перезаписывается. Больше нигде в машине сигнатура не пишется (в частности, не на
FIXING→BUILDING). retry_count здесь НЕ трогается — он инкрементируется ровно на
FIXING→BUILDING (один Agent-4-патч = одна попытка, docs §B п.3, ADR-006).

Crash-resume vs no-progress (docs §C(d), ADR-005 «ВТОРОЙ distinct failure-event»):
гард срабатывает только если та же сигнатура пришла на НОВОМ failure-event. Признак
нового события — `job.failure_event_pending` (его выставляют enter_fixing и обработчик
невалидного патча Agent 4, гард — единственный, кто сбрасывает). Если воркер упал между
записью сигнатуры гардом и завершением витка, reconciler (§E2) ре-диспетчеризует task_fix
по ТОМУ ЖЕ failure_log (pending уже сброшен) → сигнатура совпадает, но события нового нет
→ это resume, а не no_progress. Реальный no-progress (Agent 4 пропатчил/выдал тот же
невалидный патч → новый failure-event с той же сигнатурой) по-прежнему ловится.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.db.models import GenerationJob
from app.observability import metrics

# Машинные reason-коды исчерпания гардов (docs §C, полный перечень Sprint 2).
REASON_BUILD_UNRECOVERABLE = "build_unrecoverable"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
REASON_NO_PROGRESS = "no_progress"


@dataclass(frozen=True)
class GuardResult:
    """Итог проверки гардов. ok=False → джоба должна уйти в FAILED(reason)."""

    ok: bool
    reason: str | None = None


def check_fix_guards(
    job: GenerationJob,
    *,
    failure_signature: str,
    now: datetime | None = None,
) -> GuardResult:
    """Проверяет 4 гарда на входе в FIXING и записывает last_failure_signature (d).

    ПОБОЧНЫЙ ЭФФЕКТ (намеренный, docs §C(d)): при прохождении гарда no-progress
    `job.last_failure_signature` перезаписывается на `failure_signature`, а
    `job.failure_event_pending` сбрасывается в False (текущее failure-event потреблено
    гардом — crash-resume по тому же логу далее не считается no-progress). Коммит — на
    стороне вызывающего (в той же транзакции перехода в FIXING/FAILED).

    Порядок проверок (a)→(b)→(c)→(d): счётчики/таймеры дешевле и детерминированнее;
    no-progress (с записью сигнатуры) — последним, чтобы при срабатывании раннего
    гарда сигнатура не перезаписывалась впустую (джоба всё равно уходит в FAILED).
    """
    current = now or datetime.now(UTC)

    # (a) Hard cap попыток. retry_count инкрементируется на FIXING→BUILDING; на входе
    # в FIXING сравниваем уже накопленное число патчей с лимитом.
    if job.retry_count >= job.max_fix_attempts:
        return GuardResult(ok=False, reason=REASON_BUILD_UNRECOVERABLE)

    # (b) Cost cap. spend_usd — агрегат llm_usage.cost_usd (cost-ledger).
    if _as_decimal(job.spend_usd) >= _as_decimal(job.budget_usd):
        return GuardResult(ok=False, reason=REASON_BUDGET_EXHAUSTED)

    # (c) Wall-clock cap. NULL ⇒ гард выключен (в S2 всегда проставляется при создании).
    deadline = job.wall_clock_deadline
    if deadline is not None and current >= _aware(deadline):
        return GuardResult(ok=False, reason=REASON_WALL_CLOCK_EXCEEDED)

    # (d) No-progress. Срабатывает со ВТОРОГО фейла И только на НОВОМ failure-event:
    # та же сигнатура + есть непотреблённое гардом новое событие (failure_event_pending).
    # Это отличает реальный no-progress (Agent 4 пропатчил → новый фейл с той же
    # сигнатурой) от crash-resume (reconciler ре-диспетчеризовал task_fix по тому же
    # логу после краша — нового события нет, pending уже сброшен прошлым прогоном).
    same_signature = (
        job.last_failure_signature is not None and job.last_failure_signature == failure_signature
    )
    if same_signature and job.failure_event_pending:
        # Событие потреблено (гард его обработал) — даже в trip-ветке сбрасываем флаг,
        # чтобы повторная доставка той же FAILED-таски была идемпотентной.
        job.failure_event_pending = False
        # Sprint 6 (ADR-015 §2.1, TD-005): срабатывание no-progress → метрика-драйвер
        # калибровки нормализаторов сигнатуры (частота vs job_failed_total{reason=no_progress}).
        metrics.no_progress_trips_total.inc()
        return GuardResult(ok=False, reason=REASON_NO_PROGRESS)

    # Resume или прогресс: (пере)записываем сигнатуру (единственная точка, docs §C(d))
    # и потребляем текущее событие — следующий вход по тому же логу без нового события
    # (crash-resume) уже не будет считаться no-progress.
    job.last_failure_signature = failure_signature
    job.failure_event_pending = False
    return GuardResult(ok=True)


def as_decimal(value: Decimal | str | float | int) -> Decimal:
    """Приводит Decimal|str|float|int к Decimal (агрегаты spend/budget из БД могут быть str)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# Внутренний алиас (исторические вызовы в модуле).
_as_decimal = as_decimal


def _aware(dt: datetime) -> datetime:
    """Приводит naive-datetime (из БД без tz) к UTC-aware для сравнения."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
