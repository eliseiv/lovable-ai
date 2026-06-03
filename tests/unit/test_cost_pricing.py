"""Unit: расчёт стоимости вызова Claude по модели/токенам (skill claude-api, cost-ledger)."""

from __future__ import annotations

from decimal import Decimal

from app.pipeline.agents.claude_client import AgentCall, _compute_cost


def test_compute_cost_opus_known_pricing():
    # opus: input 5.00, output 25.00, cache_read 0.50, cache_write 6.25 per 1M.
    cost = _compute_cost("claude-opus-4-8", 1_000_000, 0, 0, 0)
    assert cost == Decimal("5.0000")
    cost = _compute_cost("claude-opus-4-8", 0, 1_000_000, 0, 0)
    assert cost == Decimal("25.0000")


def test_compute_cost_sonnet_includes_cache_tokens():
    cost = _compute_cost("claude-sonnet-4-6", 0, 0, 1_000_000, 1_000_000)
    # cache_read 0.30 + cache_write 3.75 = 4.05
    assert cost == Decimal("4.0500")


def test_compute_cost_unknown_model_falls_back_to_opus():
    cost_unknown = _compute_cost("mystery-model", 1_000_000, 0, 0, 0)
    cost_opus = _compute_cost("claude-opus-4-8", 1_000_000, 0, 0, 0)
    assert cost_unknown == cost_opus


def test_compute_cost_quantized_to_4dp():
    cost = _compute_cost("claude-opus-4-8", 7, 3, 0, 0)
    assert cost.as_tuple().exponent == -4


def test_agent_call_dataclass_carries_token_breakdown():
    call = AgentCall(
        text="x",
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=5,
        cache_write_tokens=2,
        cost_usd=Decimal("0.0001"),
    )
    assert call.cache_read_tokens == 5
    assert call.cache_write_tokens == 2
