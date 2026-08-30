"""Unit: маппинг модели cost-ledger → провайдер (broad-crm v1.3, `provider` в costs/daily).

Провайдер выводится из имени модели, а НЕ из `LLM_PROVIDER` инстанса: в ledger одного
инстанса сосуществуют записи обоих провайдеров (переключение по ADR-032), и подстановка
текущего провайдера переписала бы историю расходов.
"""

from __future__ import annotations

import pytest

from app.services.crm_admin_service import provider_of_model


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-4-6", "anthropic"),
        ("claude-opus-4-8", "anthropic"),
        ("CLAUDE-Sonnet-4-6", "anthropic"),
        ("gpt-5.5", "openai"),
        ("gpt-5.4-mini", "openai"),
        ("  gpt-5.5  ", "openai"),
    ],
)
def test_known_models_map_to_provider(model, expected):
    assert provider_of_model(model) == expected


def test_unknown_model_falls_back_to_raw_name():
    """Нераспознанная модель отдаётся сырым именем — CRM отнесёт её в `other`, но имя видно."""
    assert provider_of_model("llama-4-70b") == "llama-4-70b"


def test_empty_model_falls_back_to_other():
    assert provider_of_model("   ") == "other"
