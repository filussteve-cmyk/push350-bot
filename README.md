# Push350 — ES 3:50 push bot (UNPROVEN test rule set)

A new, standalone ES bot in `~/workspace/push350-bot/`. It does **not**
modify `~/workspace/s4-bot/`, and it does not start, stop, or otherwise
touch the running S4 bot.

## Status: unproven — do not trade this as a proven edge

There is no published backtest, originator, or ES win rate for "the
3:50 push". What is real is the 3:50–4:00 PM ET closing-auction window:
NYSE market-on-close / limit-on-close orders lock at 3:50 PM and the
auction imbalance is then published every second until the 4:00 PM cash
close. This bot trades level/range logic around that window. It is a
test rule set to paper-trade and measure — not a system with a verified
edge.

## The MOC-feed limitation (read this first)

**No retail aggregate NYSE/Nasdaq MOC imbalance feed is available
here, so the bot does not trade one.** `config.yaml` has
`imbalance_enabled: false`, and `bot.py` refuses to start if it is set
true without a feed reader. The bot's real trigger is the level/range
logic in `strategy.py` (Variant A/B below). Do not present the bot's
signals as imbalance-driven, and never fake an imbalance value to make
the trigger look informed.

## Strategy

- **Market/session:** RTH ES only, 1-minute trigger bars. 5-minute
  structure is derived by resampling the 1-minute bars (the resample
  assumes bars start at the 9:30 AM ET RTH open, so groups of 5 align to
  clock 5-minute boundaries). Session VWAP is RTH-anchored.
- **Bias for full size — all 3 must agree:**
  1. Price on the same side of RTH VWAP as the trade.
  2. An unmet clean target in the trade direction within 15 points
     (HOD / 3:00–3:50 range high for longs, mirror for shorts) — "unmet"
     means the target was not reclaimed during the 3:30–3:50 window
     and is still more than a tick away.
  3. The 3:00–3:50 range trending, not chopping through VWAP (>=70% of
     closes on the trade side and at most 3 VWAP crosses).
  - Half size if only 2 of 3 agree; **no trade if fewer than 2 agree.**
  - At the default size of 1 ES contract, half size rounds back to 1
    whole contract (ES cannot be halved). Set `size: 2` if you want the
    half-size rule to actually reduce to 1 contract.
- **Entry Variant A (default):** after 15:50 ET, a 1-minute close breaks
  the 3:30–3:50 range high/low on expanding volume (>=1.2x the range
  average), then retests the level and holds — enter on the retest-hold
  close.
- **Entry Variant B:** price first sweeps the opposite clean level,
  then reclaims the 3:30–3:50 range and closes back inside in the target
  direction — enter on the reclaim close.
- **Never mix variants in one run.** `config.variant` selects exactly one.
- **Timing gates (ET, hard):** entry window **15:50–15:55 only** (no new
  entry at 15:56+), **flat or breakeven-or-better by 15:58**, no
  overnight. The loop stays alive until the 15:58 flatten fires — this
  deliberately fixes the S4 latent bug where an RTH-close exit sat above
  a later flatten check.
- **Risk defaults:** 1 contract, stop 4.0 pts from entry, TP1 +6 pts,
  TP2 +9 pts, skip unless reward/risk to TP1 >= 1.5:1, max 1 trade/day
  in this window, daily realized-loss halt -$500, max 1 open position,
  no adding to losers.
- **TP1 "take 50%"** only has meaning at size >= 2. At size 1 the bot
  instead moves the stop to breakeven (entry + 1 tick in direction)
  once TP1 prints, and holds to TP2 or the stop.
- **Hard skips:** VWAP chop, target already hit before 15:50, 3:30–3:50
  range narrower than 2 pts (compressed) or wider than 30 pts
  (over-extended), or an entry that would leave under 1 minute to the
  15:58 time stop.
- **Manual skip days:** FOMC days, mega-cap earnings after the bell,
  and index rebalance days are **not** auto-detected. Set
  `manual_skip_day: true` (with a reason) in `config.yaml` on those days
  and the bot logs the skip and takes no entries.

## How to run paper

```bash
cd ~/workspace/push350-bot
# Offline smoke check first — no TopstepX auth, ZERO orders:
python3 bot.py --smoke

# Live-bar paper run (needs env credentials; forces paper mode):
export TOPSTEPX_USERNAME=... TOPSTEPX_API_KEY=... TOPSTEPX_ACCOUNT_ID=...
./run_paper.sh
```

Paper mode connects, reads bars, logs signals, and places **zero**
orders. Credentials come only from environment variables; this repo
holds placeholders only. Keys end in `=` — if you ever extract one from
a `.env` file, split on the first `=` only (`cut -d= -f2-`); truncating
the trailing `=` breaks TopstepX login. Never print a key. This bot
does not read `~/workspace/trading-bot/.env`.

## What Steve must approve before live

1. The strategy itself, after paper results — it is unproven.
2. `config.yaml`: `mode: live` **and** a real `account_id` (both are
   required; either one alone cannot go live).
3. The account: TopstepX **practice** account only, within the
   guardrails above (1 contract, -$500 daily halt, flat by 15:58).
4. The MOC-feed gap: live trading proceeds on level/range logic only,
   unless a real imbalance feed is wired in and verified.

Live execution uses client-side OCO because TopstepX rejects native
brackets: the entry is a limit order, and after it fills the bot places
a separate stop and a separate TP2 target, tracks them as an OCO pair,
and cancels the sibling when one fills. Orphaned protective orders are
cancelled on startup and on every reconcile when the broker shows flat.

## How it differs from S4

| | S4 | Push350 |
|---|---|---|
| Setup | Double top/bottom retest, all RTH | 3:50 window level/range break or sweep |
| Bars | 5-min | 1-min trigger, 5-min derived structure |
| Entry window | All RTH | 15:50–15:55 ET only |
| Flatten | 16:09 (latent 16:00-exit bug) | 15:58, flatten check above any session-end exit |
| Stop / target | 4 / 5 pts | 4 / 6 (TP1) / 9 (TP2) pts |
| Trades/day | One per level | One per day in the window |
| Daily halt | -$500 | -$500 |

## Files

- `bot.py` — async REST polling loop (12s in-window, 60s idle),
  startup reconciliation, daily state reset, 15:58 flatten, client-side
  OCO, `--smoke` offline check.
- `strategy.py` — pure functions: VWAP, ranges, bias check, Variant A/B
  triggers, trade plans, 5-min resampling.
- `risk.py` — pure guards: entry window, flatten, time-to-TP, daily
  halt, 1-trade-per-day, plus async broker helpers.
- `config.yaml` — paper default, all defaults, documented flags.
- `test_strategy.py`, `test_risk.py` — pytest suites (see below).
- `run_paper.sh` — paper launcher (forces paper mode).
- `requirements.txt` — project-x-py, pyyaml, polars, pytest.

## Verification

```bash
python3 -m pytest -q test_strategy.py test_risk.py
python3 bot.py --smoke   # offline, no auth, zero orders
```

28 tests pass. The smoke check passes (bias 3/3, Variant A trigger,
RR 1.5, risk gate ok, zero orders). No live broker authentication was
attempted during verification, and no orders were placed anywhere.
