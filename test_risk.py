"""Tests for push350 risk.py — time gates, loss halt, 1-trade-per-day."""

from datetime import datetime

import pytest

from risk import (ET, can_open, daily_loss_halted, has_time_to_tp,
                  in_entry_window, reward_risk_ok, should_flatten)


def dt(hour, minute):
    return datetime(2026, 10, 27, hour, minute, tzinfo=ET)


# --- Time gates ---------------------------------------------------------------

def test_in_entry_window():
    assert in_entry_window(dt(15, 50)) is True
    assert in_entry_window(dt(15, 55)) is True
    assert in_entry_window(dt(15, 49)) is False
    assert in_entry_window(dt(15, 56)) is False  # NO new entry after 15:55


def test_flatten_at_1558():
    assert should_flatten(dt(15, 57)) is False
    assert should_flatten(dt(15, 58)) is True
    assert should_flatten(dt(16, 0)) is True


def test_has_time_to_tp():
    assert has_time_to_tp(dt(15, 55)) is True   # 3 min to flatten
    assert has_time_to_tp(dt(15, 57)) is True   # 1 min exactly
    assert has_time_to_tp(datetime(2026, 10, 27, 15, 57, 30, tzinfo=ET)) is False


# --- can_open ------------------------------------------------------------------

def test_can_open_happy_path():
    ok, reason = can_open(False, False, 0, now=dt(15, 52), daily_pnl=0.0)
    assert ok is True and reason == "ok"


def test_no_trade_after_1555_even_if_everything_else_ok():
    ok, reason = can_open(False, False, 0, now=dt(15, 56), daily_pnl=0.0)
    assert ok is False and "window" in reason


def test_no_trade_at_flatten_time():
    ok, reason = can_open(False, False, 0, now=dt(15, 58), daily_pnl=0.0)
    assert ok is False and "flatten" in reason


def test_one_trade_per_day_blocks_second():
    ok, reason = can_open(False, False, 1, now=dt(15, 52), daily_pnl=0.0)
    assert ok is False and "already traded" in reason


def test_position_and_working_orders_block():
    ok, _ = can_open(True, False, 0, now=dt(15, 52))
    assert ok is False
    ok, _ = can_open(False, True, 0, now=dt(15, 52))
    assert ok is False


def test_daily_loss_halt():
    assert daily_loss_halted(-500.0) is True
    assert daily_loss_halted(-500.01) is True
    assert daily_loss_halted(-499.99) is False
    ok, reason = can_open(False, False, 0, now=dt(15, 52), daily_pnl=-600.0)
    assert ok is False and "halt" in reason


def test_reward_risk_gate():
    assert reward_risk_ok(100.0, 96.0, 106.0) is True    # 6/4 = 1.5
    assert reward_risk_ok(100.0, 96.0, 105.0) is False   # 5/4 = 1.25
    assert reward_risk_ok(100.0, 104.0, 94.0) is True    # short mirror
    assert reward_risk_ok(100.0, 100.0, 106.0) is False  # zero risk guard
