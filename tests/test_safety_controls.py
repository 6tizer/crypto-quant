"""安全控制测试：交易总开关 + watchdog 调度配置。"""

from __future__ import annotations

import signal
from typing import Any

import pytest


class FakeExchange:
    def __init__(self) -> None:
        self.markets: dict[str, Any] = {}
        self.load_markets_called = False

    def load_markets(self) -> None:
        self.load_markets_called = True


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def query(self, *_args: Any, **_kwargs: Any) -> "FakeQuery":
        return FakeQuery()

    def close(self) -> None:
        self.closed = True


class FakeQuery:
    def filter(self, *_args: Any, **_kwargs: Any) -> "FakeQuery":
        return self

    def scalar(self) -> int:
        return 0


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.started = False
        self.shutdown_called = False

    def add_job(self, func: Any, trigger: str, **kwargs: Any) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = False) -> None:
        self.shutdown_called = True


def test_place_market_long_returns_none_without_touching_exchange_when_trading_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """交易总开关关闭时，order_manager 内部必须直接阻止开仓。"""
    from execution import order_manager

    fake_exchange = FakeExchange()
    monkeypatch.setattr(order_manager.settings, "trading_enabled", False, raising=False)

    result = order_manager.place_market_long(
        symbol="INTC/USDT",
        exchange=fake_exchange,  # type: ignore[arg-type]
        session=object(),  # 不应被使用
        atr=0.1,
    )

    assert result is None
    assert fake_exchange.load_markets_called is False


def test_run_trading_cycle_polls_positions_but_skips_new_entries_when_trading_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """交易总开关关闭时，main 仍做持仓监控，但不读取信号、不新开仓。"""
    import main
    import execution.order_manager as order_manager
    import execution.portfolio as portfolio
    import execution.position_monitor as position_monitor
    import execution.risk_guard as risk_guard
    import data.db.models as models

    fake_session = FakeSession()
    fake_exchange = FakeExchange()
    calls: dict[str, int] = {"poll": 0, "top_signals": 0, "place": 0}

    monkeypatch.setattr(main.settings, "trading_enabled", False, raising=False)
    monkeypatch.setattr(portfolio, "get_trading_exchange", lambda: fake_exchange)
    monkeypatch.setattr(models, "get_session", lambda _url: fake_session)
    monkeypatch.setattr(risk_guard, "check_risk_status", lambda exchange=None, session=None: (True, "ok"))

    def fake_poll_positions(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        calls["poll"] += 1
        return []

    def fail_get_top_signals(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        calls["top_signals"] += 1
        raise AssertionError("get_top_signals must not be called when trading is disabled")

    def fail_place_market_long(*_args: Any, **_kwargs: Any) -> None:
        calls["place"] += 1
        raise AssertionError("place_market_long must not be called when trading is disabled")

    monkeypatch.setattr(position_monitor, "poll_positions", fake_poll_positions)
    monkeypatch.setattr(order_manager, "get_top_signals", fail_get_top_signals)
    monkeypatch.setattr(order_manager, "place_market_long", fail_place_market_long)

    main.run_trading_cycle()

    assert calls == {"poll": 1, "top_signals": 0, "place": 0}
    assert fake_session.closed is True


def test_trading_cycle_scheduler_uses_watchdog_safe_job_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """watchdog 轻量版：交易循环调度必须防止重叠堆积。"""
    import main

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(main, "BackgroundScheduler", lambda: fake_scheduler)
    monkeypatch.setattr(main, "init_db", lambda _url: None)
    monkeypatch.setattr(main, "run_market_collector", lambda: None)
    monkeypatch.setattr(main, "run_onchain_collector", lambda: None)
    monkeypatch.setattr(main, "run_fear_greed_collector", lambda: None)
    monkeypatch.setattr(signal, "pause", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    main.main()

    trading_jobs = [job for job in fake_scheduler.jobs if job.get("id") == "trading_cycle"]
    assert len(trading_jobs) == 1
    job = trading_jobs[0]
    assert job["max_instances"] == 1
    assert job["coalesce"] is True
    assert job["misfire_grace_time"] == 60
