"""Unit: парсер TOKEN_PACK_PRODUCTS + resolve_consumable_tokens (ADR-038 §B/§C, billing §11.3).

Чистая логика (без I/O):
- parse_token_pack_products: валидный CSV пар → dict[str,int]; пусто → {}; хвостовая
  запятая/пробелы игнорируются; невалидная запись (нет ':', нецелый/отрицательный amount,
  пустой product_id) → ValueError (fail-fast, ADR-038 §B).
- Settings-инстанцирование с невалидным TOKEN_PACK_PRODUCTS → приложение не стартует
  (model_validator _validate_token_pack_products → ValidationError, подкласс ValueError).
- resolve_consumable_tokens: без fallback-константы — неизвестный/отсутствующий product_id
  → None (§11.3, в отличие от resolve_tier_tokens); пак amount=0 → 0 (в мэппинге → не None).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.billing.subscription_state import resolve_consumable_tokens
from app.core.config import Settings, get_settings, parse_token_pack_products

# Канонический CSV 5 паков (ADR-038 §B / billing §11.3, дословно из ADR).
CANONICAL_CSV = (
    "100_tokens_9.99:100,250_tokens_19.99:250,500_tokens_34.99:500,"
    "1000_tokens_59.99:1000,2000_tokens_99.99:2000"
)


# ============================ parse_token_pack_products ============================


def test_parse_valid_csv_returns_dict():
    mapping = parse_token_pack_products(CANONICAL_CSV)
    assert mapping == {
        "100_tokens_9.99": 100,
        "250_tokens_19.99": 250,
        "500_tokens_34.99": 500,
        "1000_tokens_59.99": 1000,
        "2000_tokens_99.99": 2000,
    }


def test_parse_empty_string_returns_empty_mapping():
    assert parse_token_pack_products("") == {}


def test_parse_whitespace_only_returns_empty_mapping():
    assert parse_token_pack_products("   ") == {}


def test_parse_trailing_comma_ignored():
    assert parse_token_pack_products("100_tokens_9.99:100,") == {"100_tokens_9.99": 100}


def test_parse_surrounding_spaces_trimmed():
    mapping = parse_token_pack_products("  100_tokens_9.99 : 100 , 250_tokens_19.99 : 250 ")
    assert mapping == {"100_tokens_9.99": 100, "250_tokens_19.99": 250}


def test_parse_empty_segments_between_commas_skipped():
    assert parse_token_pack_products("a:1,,b:2,") == {"a": 1, "b": 2}


def test_parse_zero_amount_allowed():
    # amount >= 0 инвариант: 0 допустим (не отрицателен).
    assert parse_token_pack_products("free_pack:0") == {"free_pack": 0}


def test_parse_missing_colon_raises_value_error():
    with pytest.raises(ValueError, match="missing ':'"):
        parse_token_pack_products("no_colon_here")


def test_parse_non_int_amount_raises_value_error():
    with pytest.raises(ValueError, match="not an int"):
        parse_token_pack_products("pack:abc")


def test_parse_negative_amount_raises_value_error():
    with pytest.raises(ValueError, match="negative"):
        parse_token_pack_products("pack:-5")


def test_parse_empty_product_id_raises_value_error():
    with pytest.raises(ValueError, match="empty product_id"):
        parse_token_pack_products(":100")


# ==================== Settings fail-fast (ADR-038 §B, scenario i) ====================


def test_settings_instantiation_fails_fast_on_invalid_pack(monkeypatch):
    """Невалидный TOKEN_PACK_PRODUCTS → приложение не стартует (Settings не инстанцируется)."""
    monkeypatch.setenv("TOKEN_PACK_PRODUCTS", "broken_no_colon")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_instantiation_fails_fast_on_negative_amount(monkeypatch):
    monkeypatch.setenv("TOKEN_PACK_PRODUCTS", "pack:-1")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_instantiation_ok_on_valid_pack(monkeypatch):
    monkeypatch.setenv("TOKEN_PACK_PRODUCTS", CANONICAL_CSV)
    s = Settings()
    assert s.token_pack_map()["500_tokens_34.99"] == 500


def test_settings_instantiation_ok_on_empty(monkeypatch):
    monkeypatch.setenv("TOKEN_PACK_PRODUCTS", "")
    s = Settings()
    assert s.token_pack_map() == {}


# ==================== resolve_consumable_tokens (scenario 7) ====================


def test_resolve_known_pack_returns_amount(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    assert resolve_consumable_tokens("250_tokens_19.99", settings) == 250


def test_resolve_unknown_pack_returns_none_no_fallback(monkeypatch):
    """Неизвестный product_id → None (БЕЗ fallback-константы, в отличие от resolve_tier_tokens)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    assert resolve_consumable_tokens("com.not.a.pack", settings) is None


def test_resolve_none_vendor_product_id_returns_none(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    assert resolve_consumable_tokens(None, settings) is None


def test_resolve_empty_string_vendor_product_id_returns_none(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    assert resolve_consumable_tokens("", settings) is None


def test_resolve_zero_amount_pack_returns_zero_not_none(monkeypatch):
    """Пак amount=0 присутствует в мэппинге → 0 (не None): known, но грант short-circuit'ит."""
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", "free_pack:0", raising=False)
    assert resolve_consumable_tokens("free_pack", settings) == 0


def test_resolve_empty_mapping_returns_none(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", "", raising=False)
    assert resolve_consumable_tokens("100_tokens_9.99", settings) is None
