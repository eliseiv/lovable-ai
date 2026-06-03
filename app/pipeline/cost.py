"""Cost-ledger: запись llm_usage + агрегат spend_usd (docs/03-data-model.md).

Каждый вызов агента → строка llm_usage; сумма аккумулируется в generation_jobs.spend_usd.

Sprint 6 (ADR-015 §2.2): на каждой записи llm_usage инструментируются cost-метрики
(lovable_llm_call_cost_usd_total / _tokens_total / _cache_hit_ratio{agent,model}) — Postgres
остаётся источником истины (метрики производны). Redis budget-счётчик (TD-006) обновляется
INCRBYFLOAT через budget_cache (read-through кэш-гейт; Postgres авторитетен).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GenerationJob, LlmUsage
from app.observability import metrics
from app.pipeline.agents.claude_client import AgentCall


def _record_cost_metrics(agent: str, call: AgentCall) -> None:
    """Инструментирует cost/токены/cache-hit метрики одного вызова (ADR-015 §2.2)."""
    model = call.model
    metrics.llm_call_cost_usd_total.labels(agent=agent, model=model).inc(float(call.cost_usd))
    metrics.llm_tokens_total.labels(agent=agent, model=model, token_type="input").inc(
        call.input_tokens
    )
    metrics.llm_tokens_total.labels(agent=agent, model=model, token_type="output").inc(
        call.output_tokens
    )
    metrics.llm_tokens_total.labels(agent=agent, model=model, token_type="cache_read").inc(
        call.cache_read_tokens
    )
    metrics.llm_tokens_total.labels(agent=agent, model=model, token_type="cache_write").inc(
        call.cache_write_tokens
    )
    # cache-hit ratio = cache_read / (input + cache_read) — доля кэш-попаданий от «входа».
    denom = call.input_tokens + call.cache_read_tokens
    if denom > 0:
        metrics.llm_cache_hit_ratio.labels(agent=agent).set(call.cache_read_tokens / denom)


async def record_usage(
    session: AsyncSession,
    job: GenerationJob,
    agent: str,
    call: AgentCall,
) -> None:
    """Вставляет llm_usage и увеличивает spend_usd джобы. Коммит — у вызывающего.

    Sprint 6: после записи ledger обновляет Redis budget-кэш (TD-006, best-effort) и
    cost-метрики (ADR-015 §2.2). Postgres (spend_usd) остаётся источником истины бюджета.
    """
    session.add(
        LlmUsage(
            job_id=job.id,
            agent=agent,
            model=call.model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cache_read_tokens=call.cache_read_tokens,
            cache_write_tokens=call.cache_write_tokens,
            cost_usd=call.cost_usd,
        )
    )
    job.spend_usd = job.spend_usd + call.cost_usd
    _record_cost_metrics(agent, call)
    # Redis budget-счётчик (TD-006): INCRBYFLOAT budget:{job_id} <cost> (best-effort кэш).
    from app.observability.budget_cache import increment_budget

    await increment_budget(job.id, call.cost_usd)
