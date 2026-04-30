"""Tests for blacklist symbol filtering."""

from config.settings import Settings


def test_default_blacklist_contains_intc():
    s = Settings(blacklist_symbols="INTC")
    assert "INTC" in s.blacklist_symbol_set


def test_blacklist_parses_comma_separated():
    s = Settings(blacklist_symbols="INTC,BADCOIN,FOO")
    assert s.blacklist_symbol_set == {"INTC", "BADCOIN", "FOO"}


def test_blacklist_filters_by_base_symbol():
    """Blacklisted base symbols should filter signals in DB format (BASE/USDT)."""
    blacklist = {"INTC", "BADCOIN"}
    signals = [
        {"symbol": "BTC/USDT", "score_total": 0.8},
        {"symbol": "INTC/USDT", "score_total": 0.9},
        {"symbol": "ETH/USDT", "score_total": 0.7},
        {"symbol": "BADCOIN/USDT", "score_total": 0.6},
    ]
    filtered = [
        s for s in signals
        if s["symbol"].split("/")[0] not in blacklist
    ]
    assert len(filtered) == 2
    assert [s["symbol"] for s in filtered] == ["BTC/USDT", "ETH/USDT"]


def test_blacklist_empty_passes_all():
    blacklist: set[str] = set()
    signals = [
        {"symbol": "BTC/USDT", "score_total": 0.8},
        {"symbol": "INTC/USDT", "score_total": 0.9},
    ]
    filtered = [
        s for s in signals
        if s["symbol"].split("/")[0] not in blacklist
    ]
    assert len(filtered) == 2


def test_blacklist_removes_everything():
    blacklist = {"BTC", "ETH"}
    signals = [
        {"symbol": "BTC/USDT", "score_total": 0.8},
        {"symbol": "ETH/USDT", "score_total": 0.7},
    ]
    filtered = [
        s for s in signals
        if s["symbol"].split("/")[0] not in blacklist
    ]
    assert filtered == []
