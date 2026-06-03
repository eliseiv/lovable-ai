"""Unit: парсинг формата ключа lv_<key_id>_<secret> и распознавание нового/legacy формата.

docs/modules/auth/03-architecture.md §2, ADR-008. key_id = [a-z0-9]{16} (без `_`);
secret (token_urlsafe) может содержать `_`/`-` → режем строго по первым двум `_`.
"""

from __future__ import annotations

from app.auth.token_service import is_new_format_key, parse_api_key


def test_parse_valid_key():
    parsed = parse_api_key("lv_abcd1234efgh5678_secretpart")
    assert parsed is not None
    assert parsed.key_id == "abcd1234efgh5678"
    assert parsed.secret == "secretpart"  # noqa: S105 — это распарсенная секрет-часть, не пароль


def test_parse_secret_with_underscores_preserved():
    """secret из token_urlsafe может содержать `_`/`-`: остаток после 2-го `_` целиком."""
    parsed = parse_api_key("lv_keyid00000000000_aa_bb-cc_dd")
    assert parsed is not None
    assert parsed.key_id == "keyid00000000000"
    assert parsed.secret == "aa_bb-cc_dd"  # noqa: S105 — распарсенная секрет-часть, не пароль


def test_parse_without_prefix_returns_none():
    assert parse_api_key("qa-test-bearer-key") is None
    assert parse_api_key("Bearer lv_x_y") is None


def test_parse_missing_secret_returns_none():
    assert parse_api_key("lv_onlykeyid") is None  # нет второго `_`


def test_parse_empty_key_id_or_secret_returns_none():
    assert parse_api_key("lv__secret") is None  # пустой key_id
    assert parse_api_key("lv_keyid_") is None  # пустой secret


def test_is_new_format_key():
    assert is_new_format_key("lv_abc_def") is True
    assert is_new_format_key("legacy-seed-key") is False
    assert is_new_format_key("") is False
