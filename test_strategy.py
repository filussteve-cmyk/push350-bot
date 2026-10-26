"""Tests for push350 strategy.py — pure functions only, no broker, no clock."""

import pytest

from strategy import (Bar, bias_check, detect_variant_a, detect_variant_b,
                      make_trade, order_size_for, range_quality,
                      resample_to_5min, reward_risk_to_tp1,
                      target_already_hit, vwap)


def bar(idx, close, high=None, low=None, volume=1000.0):
    return Bar(idx=idx, open=close, high=high if high is not None else close + 0.5,
               low=low if low is not None else close - 0.5, close=close,
               volume=volume)


# --- VWAP ------------------------------------------------------------------

def test_vwap_volume_weighted():
    bars = [bar(0, 100, volume=100), bar(1, 102, volume=300)]
    # typical = close here (high/low symmetric around close)
    assert vwap(bars) == pytest.approx((100 * 100 + 102 * 300) / 400)


def test_vwap_side_long_requires_close_above_vwap():
    window = [bar(i, 101.0) for i in range(10)]
    bias = bias_check(window, 100.0, 103.0, 101.2, "long")
    assert bias.checks["vwap_side"] is True
    bias_short = bias_check(window, 100.0, 99.0, 101.2, "short")
    assert bias_short.checks["vwap_side"] is False


# --- Bias: full / half / no trade ------------------------------------------

def test_bias_full_size_when_all_three_agree():
    window = [bar(i, 101.0) for i in range(10)]  # all above VWAP 100, no crosses
    bias = bias_check(window, 100.0, 103.0, 101.2, "long")
    assert bias.agrees == 3
    assert bias.size_factor == 1.0


def test_bias_half_size_when_only_two_agree():
    # VWAP side ok, target ok, but window chops through VWAP -> trending fails.
    window = [bar(i, 101.0 if i % 2 == 0 else 99.0) for i in range(10)]
    bias = bias_check(window, 100.0, 103.0, 101.2, "long")
    assert bias.checks["vwap_side"] is True
    assert bias.checks["unmet_target"] is True
    assert bias.checks["trending"] is False
    assert bias.agrees == 2
    assert bias.size_factor == 0.5


def test_no_trade_on_vwap_chop_and_wrong_side():
    # Choppy window AND price below VWAP for a long: <2 agree.
    window = [bar(i, 101.0 if i % 2 == 0 else 99.0) for i in range(10)]
    bias = bias_check(window, 100.0, 103.0, 98.0, "long")
    assert bias.size_factor == 0.0


def test_no_trade_when_target_already_hit_definition():
    # Target within a tick of price (already there) is not "unmet".
    window = [bar(i, 101.0) for i in range(10)]
    bias = bias_check(window, 100.0, 101.2, 101.2, "long")
    assert bias.checks["unmet_target"] is False


def test_target_already_hit_in_late_window():
    late = [bar(0, 101.0, high=103.0), bar(1, 101.0, high=101.5)]
    assert target_already_hit(late, 103.0, "long") is True
    assert target_already_hit(late, 104.0, "long") is False
    late_short = [bar(0, 101.0, low=99.0)]
    assert target_already_hit(late_short, 99.0, "short") is True


def test_range_quality_compressed_and_over_extended():
    ok, _ = range_quality(102.0, 100.0)
    assert ok is True
    ok, reason = range_quality(101.0, 100.0)
    assert ok is False and "compressed" in reason
    ok, reason = range_quality(140.0, 100.0)
    assert ok is False and "over-extended" in reason


def test_order_size_for():
    assert order_size_for(2, 1.0) == 2
    assert order_size_for(2, 0.5) == 1
    assert order_size_for(1, 0.5) == 1  # documented rounding at size 1
    assert order_size_for(1, 0.0) == 0


# --- Variant A: break + retest hold -----------------------------------------

def test_variant_a_long_break_retest_hold():
    post = [
        bar(0, 102.5, high=102.6, low=102.0, volume=1500.0),  # break, expanding
        bar(1, 102.6, high=102.8, low=101.8, volume=900.0),   # retest holds
    ]
    trig = detect_variant_a(post, range_high=102.0, range_low=100.0,
                            range_avg_volume=1000.0)
    assert trig is not None
    assert trig.direction == "long" and trig.variant == "A"
    assert trig.entry == 102.6
    assert trig.stop == pytest.approx(98.6)
    assert trig.tp1 == pytest.approx(108.6)


def test_variant_a_no_break_without_expanding_volume():
    post = [
        bar(0, 102.5, high=102.6, low=102.0, volume=500.0),  # break, thin volume
        bar(1, 102.6, high=102.8, low=101.8, volume=900.0),
    ]
    trig = detect_variant_a(post, range_high=102.0, range_low=100.0,
                            range_avg_volume=1000.0)
    assert trig is None


def test_variant_a_no_retest_if_level_lost():
    post = [
        bar(0, 102.5, high=102.6, low=102.0, volume=1500.0),
        bar(1, 101.0, high=102.0, low=100.5, volume=900.0),  # falls through level
    ]
    trig = detect_variant_a(post, range_high=102.0, range_low=100.0,
                            range_avg_volume=1000.0)
    assert trig is None


def test_variant_a_short_mirror():
    post = [
        bar(0, 99.5, high=100.0, low=99.4, volume=1500.0),
        bar(1, 99.4, high=100.2, low=99.2, volume=900.0),
    ]
    trig = detect_variant_a(post, range_high=102.0, range_low=100.0,
                            range_avg_volume=1000.0)
    assert trig is not None and trig.direction == "short"


# --- Variant B: sweep + reclaim ----------------------------------------------

def test_variant_b_long_sweep_reclaim():
    post = [
        bar(0, 99.5, high=100.0, low=99.0, volume=1000.0),   # sweep below 100
        bar(1, 100.6, high=100.8, low=100.2, volume=1000.0),  # reclaim inside
    ]
    trig = detect_variant_b(post, range_high=102.0, range_low=100.0)
    assert trig is not None
    assert trig.direction == "long" and trig.variant == "B"
    assert trig.entry == 100.6


def test_variant_b_no_reclaim_no_trigger():
    post = [bar(0, 99.5, high=100.0, low=99.0, volume=1000.0)]
    assert detect_variant_b(post, range_high=102.0, range_low=100.0) is None


def test_variant_b_short_mirror():
    post = [
        bar(0, 102.5, high=103.0, low=102.0, volume=1000.0),
        bar(1, 101.4, high=101.8, low=101.2, volume=1000.0),
    ]
    trig = detect_variant_b(post, range_high=102.0, range_low=100.0)
    assert trig is not None and trig.direction == "short"


# --- Trade plan / resampling --------------------------------------------------

def test_reward_risk_to_tp1():
    trade = make_trade(100.0, "long")  # stop 96, tp1 106 -> 6/4 = 1.5
    assert reward_risk_to_tp1(trade) == pytest.approx(1.5)
    trade_short = make_trade(100.0, "short")
    assert reward_risk_to_tp1(trade_short) == pytest.approx(1.5)


def test_resample_to_5min():
    bars = [bar(i, 100.0 + i * 0.1, high=101.0, low=99.0) for i in range(10)]
    out = resample_to_5min(bars)
    assert len(out) == 2
    assert out[0].close == pytest.approx(100.4)
    assert out[1].open == pytest.approx(100.5)
