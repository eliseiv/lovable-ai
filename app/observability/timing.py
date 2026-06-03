"""Тайминг Claude-вызовов агентов (Sprint 6, ADR-015 §2.2).

Контекст-менеджер для измерения latency одного Claude-вызова с записью в метрику
lovable_llm_call_latency_seconds{agent,model}. Вынесен из claude_client.run_agent, чтобы
не менять его сигнатуру (агент-метка известна в обёртках agent1..4, а не в run_agent).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.observability import metrics


@contextmanager
def timed_agent_call(agent: str, model: str) -> Iterator[None]:
    """Замеряет latency Claude-вызова и пишет lovable_llm_call_latency_seconds{agent,model}."""
    started = time.monotonic()
    try:
        yield
    finally:
        metrics.llm_call_latency_seconds.labels(agent=agent, model=model).observe(
            time.monotonic() - started
        )
