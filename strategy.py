"""Push350 strategy — pure functions, no I/O, no clock, no broker calls.

Implements the researched 3:50 push test rule set, honestly labelled
UNPROVEN: there is no published backtest, originator, or ES win rate for
"the 3:50 push". What is real is the 3:50-4:00 PM ET closing-auction
window (NYSE MOC/LOC lock at 3:50 PM, imbalance published every second
thereafter). This bot trades the level/range logic around that window;
it does NOT trade a real MOC imbalance feed, because no retail aggregate
NYSE/Nasdaq MOC imbalance feed is available here (see README and
config.yaml: ``imbalance_enabled`` is off by default and must stay off
unless a real feed is wired in — never fake one).

Rules implemented here (defaults in config.yaml):
  - RTH ES only. 1-minute trigger bars; 5-minute structure is derived by
    resampling the 1-minute bars (see resample_to_5min).
  - Session VWAP is RTH-anchored (from the 9:30 AM ET open).
  - Bias for FULL size requires all 3 checks to agree:
      (1) price on the same side of RTH VWAP as the trade,
      (2) an unmet clean target in the trade direction within reach
          (HOD / 3:00-3:50 range high for longs, mirror for shorts),
      (3) the 3:00-3:50 range trending, not chopping through VWAP.
    Half size if only 2 of 3 agree; NO trade if fewer than 2 agree.
  - "Unmet target" definition used here (read this before tuning):
    the target is a level set BEFORE the late window that price has NOT
    reclaimed during the 3:30-3:50 window and is still below/above the
    current price by more than a tick but no more than MAX_TARGET_DISTANCE
    points. A morning HOD that price already reclaimed at 3:40 PM is not
    a fresh target — that is the "target already hit" hard skip.
  - Entry Variant A (default): after 15:50 ET, a 1-minute close breaks
    the 3:30-3:50 range high/low on expanding volume, then retests the
    level and holds — enter on the retest-hold close.
  - Entry Variant B: price first sweeps the OPPOSITE clean level, then
    reclaims the 3:30-3:50 range and closes back inside in the target
    direction — enter on the reclaim close.
  - Never mix variants in one run: config selects exactly one.
  - Risk (see risk.py for time gates): 1 contract default, stop 4.0 pts
    from entry, TP1 +6 pts, TP2 +9 pts, >=1.5:1 reward/risk to TP1 or
    skip, max 1 trade/day in this window, daily realized-loss halt -$500.
  - TP1 "take 50%" only has meaning at size >= 2. At the default size of
    1 ES contract the bot instead moves the stop to breakeven once TP1
    prints (see bot.py); likewise "half size" at size 1 rounds back to
    1 whole ES contract — set size=2 if you want the half-size rule to
    actually reduce to 1 contract (see order_size_for).
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

TICK = 0.25
STOP_POINTS = 4.0
TP1_POINTS = 6.0
TP2_POINTS = 9.0
MIN_RR_TO_TP1 = 1.5
MAX_TARGET_DISTANCE = 15.0   # target must be within 15 pts of price
MIN_RANGE_WIDTH = 2.0        # 3:30-3:50 range narrower than this = compressed
MAX_RANGE_WIDTH = 30.0       # wider than this = already over-extended
TREND_MIN_SIDE_FRACTION = 0.70  # >=70% of 3:00-3:50 closes on trade side
TREND_MAX_VWAP_CROSSES = 3      # more VWAP crosses than this = chop
VOLUME_EXPANSION = 1.2          # break volume must exceed 1.2x range avg
RETEST_TOLERANCE = 0.5          # retest low may dip at most 0.5 below level
SWEEP_TOLERANCE = 0.25          # sweep must exceed level by at least a tick


@dataclass
class Bar:
    """One 1-minute (or resampled 5-minute) bar. idx is sequential."""
    idx: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class BiasResult:
    direction: str            # "long" | "short"
    agrees: int               # 0..3
    size_factor: float        # 1.0 if 3/3, 0.5 if 2/3, 0.0 if <2
    target: float
    target_distance: float
    checks: dict              # {"vwap_side": bool, "unmet_target": bool, "trending": bool}


@dataclass
class Trigger:
    direction: str            # "long" | "short"
    variant: str              # "A" | "B"
    entry: float
    stop: float
    tp1: float
    tp2: float
    reason: str


# ---------------------------------------------------------------------------
# VWAP / ranges / resampling
# ---------------------------------------------------------------------------

def typical_price(bar: Bar) -> float:
    return (bar.high + bar.low + bar.close) / 3.0


def vwap(bars: List[Bar]) -> Optional[float]:
    """RTH-anchored VWAP over the bars given (caller passes bars from open)."""
    if not bars:
        return None
    vol = sum(b.volume for b in bars)
    if vol > 0:
        return sum(typical_price(b) * b.volume for b in bars) / vol
    return sum(typical_price(b) for b in bars) / len(bars)


def session_high_low(bars: List[Bar]) -> Tuple[Optional[float], Optional[float]]:
    if not bars:
        return None, None
    return max(b.high for b in bars), min(b.low for b in bars)


def range_high_low(bars: List[Bar]) -> Tuple[Optional[float], Optional[float]]:
    if not bars:
        return None, None
    return max(b.high for b in bars), min(b.low for b in bars)


def resample_to_5min(bars_1m: List[Bar]) -> List[Bar]:
    """Resample 1-minute bars to 5-minute bars.

    Assumes bars_1m starts at the 9:30 AM ET RTH open, so groups of 5
    align to clock 5-minute boundaries. Returns one Bar per full group
    of 5 (a trailing partial group is dropped — it is not a closed bar).
    """
    out: List[Bar] = []
    for i in range(0, len(bars_1m) - 4, 5):
        grp = bars_1m[i:i + 5]
        out.append(Bar(idx=len(out), open=grp[0].open, high=max(b.high for b in grp),
                       low=min(b.low for b in grp), close=grp[-1].close,
                       volume=sum(b.volume for b in grp)))
    return out


def vwap_crossings(bars: List[Bar], vwap_value: float) -> int:
    """Count sign changes of (close - VWAP) across consecutive bars."""
    crosses = 0
    prev = None
    for b in bars:
        side = 1 if b.close > vwap_value else (-1 if b.close < vwap_value else 0)
        if prev is not None and side != 0 and prev != 0 and side != prev:
            crosses += 1
        if side != 0:
            prev = side
    return crosses


# ---------------------------------------------------------------------------
# Bias check (3 checks -> full / half / no trade)
# ---------------------------------------------------------------------------

def bias_check(window_bars: List[Bar], vwap_value: float,
               target: float, last_close: float,
               direction: str) -> BiasResult:
    """Score the 3 bias checks for one direction.

    window_bars: 3:00-3:50 PM ET 1-minute bars. target: HOD (long) or LOD
    (short) — callers pass max(HOD, range high) / min(LOD, range low) as
    the clean target; the unmet test below is what makes it "unmet".
    """
    if direction == "long":
        vwap_ok = last_close > vwap_value
        distance = target - last_close
        side_fraction = (sum(1 for b in window_bars if b.close > vwap_value)
                         / len(window_bars)) if window_bars else 0.0
    else:
        vwap_ok = last_close < vwap_value
        distance = last_close - target
        side_fraction = (sum(1 for b in window_bars if b.close < vwap_value)
                         / len(window_bars)) if window_bars else 0.0
    unmet_ok = TICK < distance <= MAX_TARGET_DISTANCE
    trending_ok = (bool(window_bars)
                   and side_fraction >= TREND_MIN_SIDE_FRACTION
                   and vwap_crossings(window_bars, vwap_value) <= TREND_MAX_VWAP_CROSSES)
    checks = {"vwap_side": bool(vwap_ok), "unmet_target": bool(unmet_ok),
              "trending": bool(trending_ok)}
    agrees = sum(1 for v in checks.values() if v)
    size_factor = 1.0 if agrees == 3 else (0.5 if agrees == 2 else 0.0)
    return BiasResult(direction=direction, agrees=agrees, size_factor=size_factor,
                      target=target, target_distance=distance, checks=checks)


def target_already_hit(late_window_bars: List[Bar], target: float,
                       direction: str) -> bool:
    """True if price already reclaimed the target during 3:30-3:50.

    Long: any high at/above target. Short: any low at/below target.
    A target reclaimed before the entry window is not a fresh target.
    """
    if direction == "long":
        return any(b.high >= target - TICK for b in late_window_bars)
    return any(b.low <= target + TICK for b in late_window_bars)


def range_quality(range_high: float, range_low: float) -> Tuple[bool, str]:
    """Hard-skip check for a compressed or over-extended 3:30-3:50 range."""
    width = range_high - range_low
    if width < MIN_RANGE_WIDTH:
        return False, f"range too compressed ({width:.2f} pts < {MIN_RANGE_WIDTH})"
    if width > MAX_RANGE_WIDTH:
        return False, f"range over-extended ({width:.2f} pts > {MAX_RANGE_WIDTH})"
    return True, "ok"


def order_size_for(size: int, size_factor: float) -> int:
    """Whole-contract size for a bias factor.

    size_factor 1.0 -> size; 0.5 -> half, rounded to whole contracts with
    a floor of 1 (so size=1 at half factor stays 1 — see module docstring).
    size_factor 0.0 -> 0 (no trade).
    """
    if size_factor <= 0.0:
        return 0
    return max(1, round(size * size_factor))


# ---------------------------------------------------------------------------
# Trade plan / reward-risk
# ---------------------------------------------------------------------------

def make_trade(entry: float, direction: str,
               stop_points: float = STOP_POINTS,
               tp1_points: float = TP1_POINTS,
               tp2_points: float = TP2_POINTS) -> Trigger:
    if direction == "long":
        return Trigger(direction="long", variant="", entry=entry,
                       stop=entry - stop_points, tp1=entry + tp1_points,
                       tp2=entry + tp2_points, reason="")
    return Trigger(direction="short", variant="", entry=entry,
                   stop=entry + stop_points, tp1=entry - tp1_points,
                   tp2=entry - tp2_points, reason="")


def reward_risk_to_tp1(trade: Trigger) -> float:
    risk = abs(trade.entry - trade.stop)
    if risk == 0:
        return 0.0
    return abs(trade.tp1 - trade.entry) / risk


# ---------------------------------------------------------------------------
# Variant A: range break (expanding volume) + retest hold
# ---------------------------------------------------------------------------

def detect_variant_a(post_bars: List[Bar], range_high: float, range_low: float,
                     range_avg_volume: float) -> Optional[Trigger]:
    """Scan post-15:50 1-minute bars for a break + retest hold, either side.

    Long: a close above range_high with volume >= 1.2x the range average,
    then a later bar that dips to within RETEST_TOLERANCE below range_high
    but closes back above it (holds). Mirror image for shorts.
    Returns the first qualifying trigger, or None.
    """
    for side in ("long", "short"):
        broke = False
        for b in post_bars:
            if range_avg_volume > 0 and b.volume < VOLUME_EXPANSION * range_avg_volume:
                # Volume filter applies to the break bar only; retest bars
                # are checked below regardless of volume.
                if not broke:
                    continue
            if not broke:
                if side == "long" and b.close > range_high:
                    broke = True
                elif side == "short" and b.close < range_low:
                    broke = True
                continue
            # Retest-hold bar.
            if side == "long":
                if (range_high - RETEST_TOLERANCE <= b.low <= range_high + 1.0
                        and b.close > range_high):
                    t = make_trade(b.close, "long")
                    t.variant, t.reason = "A", "range break + retest hold (long)"
                    return t
            else:
                if (range_low - 1.0 <= b.high <= range_low + RETEST_TOLERANCE
                        and b.close < range_low):
                    t = make_trade(b.close, "short")
                    t.variant, t.reason = "A", "range break + retest hold (short)"
                    return t
    return None


# ---------------------------------------------------------------------------
# Variant B: sweep of the opposite level, then reclaim back inside
# ---------------------------------------------------------------------------

def detect_variant_b(post_bars: List[Bar], range_high: float,
                     range_low: float) -> Optional[Trigger]:
    """Scan post-15:50 bars for sweep-then-reclaim, either side.

    Long: a bar sweeps below range_low by at least a tick, then a later
    bar closes back above range_low (inside the range). Mirror for shorts.
    """
    for side in ("long", "short"):
        swept = False
        for b in post_bars:
            if not swept:
                if side == "long" and b.low < range_low - SWEEP_TOLERANCE:
                    swept = True
                elif side == "short" and b.high > range_high + SWEEP_TOLERANCE:
                    swept = True
                continue
            if side == "long" and b.close > range_low:
                t = make_trade(b.close, "long")
                t.variant, t.reason = "B", "sweep below range low + reclaim (long)"
                return t
            if side == "short" and b.close < range_high:
                t = make_trade(b.close, "short")
                t.variant, t.reason = "B", "sweep above range high + reclaim (short)"
                return t
    return None
