"""Tests for scripts/verify_stage3.py"""

from types import SimpleNamespace

import scripts.verify_stage3 as v


class DummySession:
    def close(self):
        pass


def test_main_returns_zero_when_all_checks_pass(monkeypatch, capsys):
    monkeypatch.setattr(v, "get_session", lambda _: DummySession())
    monkeypatch.setattr(v, "get_runtime_stats", lambda s: {"运行时长": "1.0 小时", "总交易笔数": 1, "已平仓": 1, "持仓中": 0, "胜率": "100%"})
    monkeypatch.setattr(
        v,
        "check_stop_loss",
        lambda s: {"name": "止损触发", "passed": True, "detail": "1 次"},
    )
    monkeypatch.setattr(
        v,
        "check_take_profit",
        lambda s: {"name": "止盈阶梯触发", "passed": True, "detail": "1 次"},
    )
    monkeypatch.setattr(
        v,
        "check_timeout_close",
        lambda s: {"name": "48h 兜底平仓", "passed": True, "detail": "1 次"},
    )
    monkeypatch.setattr(
        v,
        "check_risk_pause",
        lambda s: {"name": "风控暂停", "passed": True, "detail": "1 次"},
    )
    monkeypatch.setattr(
        v,
        "check_tg_notifications",
        lambda: {"name": "Telegram 通知", "passed": True, "detail": "1 条"},
    )
    monkeypatch.setattr(
        v,
        "check_watchdog",
        lambda: {"name": "Watchdog", "passed": True, "detail": "正常"},
    )
    monkeypatch.setattr(
        v,
        "check_dashboard_push",
        lambda s: {"name": "数据看板推送", "passed": True, "detail": "1 次"},
    )

    rc = v.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "总计: 7/7 项通过" in out


def test_main_returns_one_when_some_checks_fail(monkeypatch, capsys):
    monkeypatch.setattr(v, "get_session", lambda _: DummySession())
    monkeypatch.setattr(v, "get_runtime_stats", lambda s: {"运行时长": "1.0 小时", "总交易笔数": 0, "已平仓": 0, "持仓中": 0, "胜率": "0%"})

    # 1 pass + 6 fail
    monkeypatch.setattr(v, "check_stop_loss", lambda s: {"name": "止损触发", "passed": False, "detail": "0 次"})
    monkeypatch.setattr(v, "check_take_profit", lambda s: {"name": "止盈阶梯触发", "passed": False, "detail": "0 次"})
    monkeypatch.setattr(v, "check_timeout_close", lambda s: {"name": "48h 兜底平仓", "passed": False, "detail": "0 次"})
    monkeypatch.setattr(v, "check_risk_pause", lambda s: {"name": "风控暂停", "passed": False, "detail": "0 次"})
    monkeypatch.setattr(v, "check_tg_notifications", lambda: {"name": "Telegram 通知", "passed": False, "detail": "0 条"})
    monkeypatch.setattr(v, "check_watchdog", lambda: {"name": "Watchdog", "passed": True, "detail": "正常"})
    monkeypatch.setattr(v, "check_dashboard_push", lambda s: {"name": "数据看板推送", "passed": False, "detail": "0 次"})

    rc = v.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "总计: 1/7 项通过" in out
