"""Tests for utils/symbol.py — 符号格式转换."""

from utils.symbol import to_db_symbol, to_exchange_symbol


# ── to_exchange_symbol ──────────────────────────────────────────


def test_to_exchange_symbol_basic():
    assert to_exchange_symbol("BTC/USDT") == "BTC/USDT:USDT"


def test_to_exchange_symbol_idempotent():
    assert to_exchange_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_to_exchange_symbol_altcoin():
    assert to_exchange_symbol("SKYAI/USDT") == "SKYAI/USDT:USDT"


def test_to_exchange_symbol_already_suffixed():
    result = to_exchange_symbol("ETH/USDT:USDT")
    assert result == "ETH/USDT:USDT"


def test_to_exchange_symbol_empty_string():
    """Empty string still gets :USDT appended — edge case."""
    assert to_exchange_symbol("") == ":USDT"


# ── to_db_symbol ────────────────────────────────────────────────


def test_to_db_symbol_basic():
    assert to_db_symbol("BTC/USDT:USDT") == "BTC/USDT"


def test_to_db_symbol_idempotent():
    assert to_db_symbol("BTC/USDT") == "BTC/USDT"


def test_to_db_symbol_altcoin():
    assert to_db_symbol("SKYAI/USDT:USDT") == "SKYAI/USDT"


def test_to_db_symbol_empty_string():
    assert to_db_symbol("") == ""


# ── round-trip ──────────────────────────────────────────────────


def test_roundtrip_db_to_exchange_to_db():
    original = "BTC/USDT"
    assert to_db_symbol(to_exchange_symbol(original)) == original


def test_roundtrip_exchange_to_db_to_exchange():
    original = "BTC/USDT:USDT"
    assert to_exchange_symbol(to_db_symbol(original)) == original
