"""Unit: OpenAI cost-расчёт по нормативной pricing-таблице (ADR-032 §4, observability §2.2A).

Источник чисел — docs/modules/observability/03-architecture.md §2.2A (ЕДИНСТВЕННЫЙ нормативный
источник OpenAI-pricing, per-1M USD, верифицировано 2026-06-15). docs/06-testing-strategy.md
§Unit «OpenAI usage/cost-маппинг».

Покрывает сценарий 3 ТЗ (cost-часть):
- дефолтные модели gpt-5.4-mini / gpt-5.5 считаются точным числом по §2.2A;
- cache_read тарифицируется по cached_input-ставке; cache_write-член = 0 (OpenAI без write-ставки);
- неизвестная модель → консервативный fallback (самый дорогой output-тариф, аналог Anthropic→Opus);
- результат квантуется до 4 знаков.

Числа §2.2A (per-1M USD): gpt-5.5 input 5.00 / cache_read 0.50 / output 30.00;
gpt-5.4-mini input 0.75 / cache_read 0.075 / output 4.50; gpt-5.5-pro (fallback) output 180.00.
"""

from __future__ import annotations

from decimal import Decimal

from app.pipeline.agents.openai_client import _FALLBACK_MODEL, _compute_cost

# --- дефолтные модели: точное число по §2.2A ---


def test_cost_gpt55_input_output_exact():
    # gpt-5.5: input 5.00, output 30.00 per 1M.
    assert _compute_cost("gpt-5.5", 1_000_000, 0, 0, 0) == Decimal("5.0000")
    assert _compute_cost("gpt-5.5", 0, 1_000_000, 0, 0) == Decimal("30.0000")


def test_cost_gpt55_cache_read_exact():
    # gpt-5.5 cached_input (cache_read) 0.50 per 1M.
    assert _compute_cost("gpt-5.5", 0, 0, 1_000_000, 0) == Decimal("0.5000")


def test_cost_gpt54_mini_input_output_exact():
    # gpt-5.4-mini: input 0.75, output 4.50, cache_read 0.075 per 1M.
    assert _compute_cost("gpt-5.4-mini", 1_000_000, 0, 0, 0) == Decimal("0.7500")
    assert _compute_cost("gpt-5.4-mini", 0, 1_000_000, 0, 0) == Decimal("4.5000")
    assert _compute_cost("gpt-5.4-mini", 0, 0, 1_000_000, 0) == Decimal("0.0750")


def test_cost_gpt54_mini_combined():
    # Смешанный вызов: input 500k + output 200k + cache_read 100k.
    # 0.75*0.5 + 4.50*0.2 + 0.075*0.1 = 0.375 + 0.900 + 0.0075 = 1.2825.
    cost = _compute_cost("gpt-5.4-mini", 500_000, 200_000, 100_000, 0)
    assert cost == Decimal("1.2825")


# --- cache_write всегда 0 для OpenAI (§4/§6) ---


def test_cost_cache_write_member_is_zero():
    """cache_write-токены НЕ добавляют к стоимости (OpenAI caching без write-ставки, §6)."""
    without = _compute_cost("gpt-5.5", 1_000_000, 0, 0, 0)
    with_write = _compute_cost("gpt-5.5", 1_000_000, 0, 0, 9_999_999)
    assert without == with_write == Decimal("5.0000")


# --- неизвестная модель → консервативный fallback ---


def test_cost_unknown_model_falls_back_to_most_expensive():
    """Неизвестный AGENTn_MODEL → fallback на самый дорогой output-тариф (§4, не занижаем)."""
    cost_unknown = _compute_cost("gpt-9-imaginary", 0, 1_000_000, 0, 0)
    cost_fallback = _compute_cost(_FALLBACK_MODEL, 0, 1_000_000, 0, 0)
    assert cost_unknown == cost_fallback
    # _FALLBACK_MODEL = gpt-5.5-pro → output 180.00 per 1M (§2.2A).
    assert cost_unknown == Decimal("180.0000")


def test_cost_quantized_to_4dp():
    cost = _compute_cost("gpt-5.4-mini", 7, 3, 1, 0)
    assert cost.as_tuple().exponent == -4
