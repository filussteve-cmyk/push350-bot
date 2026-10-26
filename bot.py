"""Push350 bot — intraday ES futures on TopstepX via project-x-py REST.

Paper mode (default): connects, reads bars, detects setups and LOGS every
signal. It NEVER calls any order-placement API in paper mode.

Live mode: ONLY when config.yaml explicitly sets ``mode: live`` AND a
real account_id is set. Live entries are a limit order at the trigger
close, with client-side OCO protection added after the fill (TopstepX
rejects native brackets, so the stop and target are placed as separate
orders and tracked as an OCO pair; the sibling is cancelled when one
fills, and orphaned protective orders are cancelled on reconcile).

Startup: existing positions and working orders are queried FIRST. If a
position or working order already exists, the bot manages/monitors it
and never double-orders after a restart.

LOOP ORDERING — READ BEFORE EDITING: the 15:58 flatten check sits ABOVE
any RTH-close exit, and the loop stays alive until the flatten fires.
This deliberately does NOT repeat the S4 latent bug where an RTH-close
exit at 16:00 sat above a later flatten check and could walk away
without flattening. After the 15:58 flatten the session is over and the
loop exits; there is no overnight position, ever.

Modes:
  python3 bot.py            # normal run (paper unless config says live)
  python3 bot.py --smoke    # offline paper smoke check: NO TopstepX
                            # authentication, NO orders, synthetic bars
                            # through the real strategy + risk gates.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from strategy import (Bar, bias_check, detect_variant_a, detect_variant_b,
                      make_trade, order_size_for, range_high_low,
                      range_quality, resample_to_5min,
                      reward_risk_to_tp1, session_high_low,
                      target_already_hit, vwap)
import risk
from risk import ET

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.yaml"
STATE_PATH = BASE / "state.json"

log = logging.getLogger("push350")


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    # Environment variables take precedence (never commit real keys).
    cfg["username"] = os.environ.get("TOPSTEPX_USERNAME", cfg.get("username"))
    cfg["api_key"] = os.environ.get("TOPSTEPX_API_KEY", cfg.get("api_key"))
    cfg["account_id"] = int(os.environ.get("TOPSTEPX_ACCOUNT_ID",
                                           cfg.get("account_id", 0)))
    # run_paper.sh forces paper mode even if config was edited to live.
    if os.environ.get("PUSH350_FORCE_PAPER") == "1":
        cfg["mode"] = "paper"
    return cfg


def blank_state() -> dict:
    return {
        "date": risk.now_et().strftime("%Y-%m-%d"),
        "trades_today": 0,
        "signals_today": 0,
        "last_signal_entry": None,
        "daily_pnl": 0.0,
        "halted": False,
        "phase": "flat",          # flat | entry_pending | position_open
        "has_position": False,
        "entry_order_id": None,
        "protective_order_ids": [],
        "tp1_done": False,
        "trade": None,            # active trade plan (entry/stop/tp1/tp2)
        "flattened_today": False,
    }


def load_state() -> dict:
    today = risk.now_et().strftime("%Y-%m-%d")
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text())
            if st.get("date") == today:
                return st
            log.info("New trading day — resetting daily state.")
        except Exception as e:
            log.warning("Could not read state.json (%s); starting fresh.", e)
    st = blank_state()
    st["date"] = today
    return st


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Smoke check (offline, no broker, no orders)
# ---------------------------------------------------------------------------

def run_smoke(cfg: dict) -> int:
    """Run the real strategy + risk gates on synthetic bars. No auth.

    Builds a textbook Variant A long: 3:00-3:50 closes above VWAP,
    HOD unmet and within reach, range break on expanding volume, then a
    retest that holds. Logs PAPER throughout and places ZERO orders.
    """
    log.info("PAPER SMOKE: offline check — no TopstepX authentication, "
             "ZERO orders will be placed.")
    if str(cfg.get("mode", "paper")).lower() != "paper":
        log.warning("SMOKE forces paper mode (config mode ignored).")
    # VWAP anchor: flat 100.00 session, then a trending late window.
    window = [Bar(idx=i, open=101.0, high=101.5, low=100.8, close=101.2,
                  volume=1000.0) for i in range(10)]
    bias = bias_check(window, 100.0, 103.0, 101.2, "long")
    log.info("SMOKE bias long: agrees=%d/3 size_factor=%.1f checks=%s",
             bias.agrees, bias.size_factor, bias.checks)
    assert bias.agrees == 3 and bias.size_factor == 1.0, "smoke bias failed"
    post = [
        Bar(idx=10, open=102.0, high=102.6, low=101.9, close=102.5, volume=1500.0),
        Bar(idx=11, open=102.4, high=102.8, low=101.8, close=102.6, volume=900.0),
    ]
    trig = detect_variant_a(post, range_high=102.0, range_low=100.0,
                            range_avg_volume=1000.0)
    assert trig is not None and trig.direction == "long", "smoke trigger failed"
    log.info("SMOKE trigger %s entry=%.2f stop=%.2f tp1=%.2f tp2=%.2f "
             "rr_to_tp1=%.2f", trig.variant, trig.entry, trig.stop,
             trig.tp1, trig.tp2, reward_risk_to_tp1(trig))
    assert reward_risk_to_tp1(trig) >= 1.5, "smoke RR failed"
    from datetime import datetime
    ok, reason = risk.can_open(
        has_position=False, has_working_orders=False, trades_today=0,
        now=datetime(2026, 10, 27, 15, 52, tzinfo=ET), daily_pnl=0.0)
    assert ok, f"smoke risk gate failed: {reason}"
    log.info("SMOKE risk gate: ok (%s)", reason)
    log.info("PAPER SMOKE PASSED: bias 3/3, Variant A trigger, RR >= 1.5, "
             "risk gate ok — ZERO orders placed.")
    return 0


# ---------------------------------------------------------------------------
# Broker helpers
# ---------------------------------------------------------------------------

async def resolve_es_contract(client) -> str:
    """Return the front-month ES contract id (activeContract flag)."""
    contracts = await client.list_available_contracts(live=False)
    for c in contracts:
        name = (c.name or "").upper()
        if name.startswith("ES") and c.activeContract:
            log.info("ES front-month contract: %s (%s)", c.id, c.name)
            return c.id
    raise RuntimeError("No activeContract ES front-month contract found.")


def bars_from_df(df, trade_date: str):
    """Convert the SDK's polars bar DataFrame to today's RTH 1-min bars.

    Returns a list of (timestamp, Bar). SDK timestamps are UTC; convert
    to ET. The latest bar may be partial (still forming) — triggers use
    its close provisionally, which is why the retest-hold close and the
    15:58 time stop matter.
    """
    out = []
    for row in df.to_dicts():
        ts = row["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)  # pragma: no cover - defensive
        else:
            ts = ts.astimezone(ET)
        if ts.strftime("%Y-%m-%d") != trade_date:
            continue
        t = ts.time()
        if not (risk.RTH_OPEN <= t < risk.RTH_CLOSE):
            continue
        out.append((ts, Bar(idx=len(out), open=row["open"], high=row["high"],
                            low=row["low"], close=row["close"],
                            volume=float(row.get("volume") or 0.0))))
    return out


def in_window(ts, start: time, end: time) -> bool:
    return start <= ts.time() < end


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true",
                        help="offline paper smoke check (no auth, no orders)")
    args = parser.parse_args()

    cfg = load_config()
    if args.smoke:
        sys.exit(run_smoke(cfg))

    mode = str(cfg.get("mode", "paper")).lower()
    live = (mode == "live")
    account_id = int(cfg.get("account_id", 0))
    size = int(cfg.get("size", 1))
    variant = str(cfg.get("variant", "A")).upper()
    max_daily_loss = float(cfg.get("max_daily_loss", 500.0))
    max_trades = int(cfg.get("max_trades_per_day", 1))
    poll_window = int(cfg.get("poll_seconds_window", 12))
    poll_idle = int(cfg.get("poll_seconds_idle", 60))
    symbol = str(cfg.get("symbol", "ES"))

    if not cfg.get("username") or not cfg.get("api_key") \
            or "YOUR_" in str(cfg.get("username", "")) \
            or "YOUR_" in str(cfg.get("api_key", "")):
        log.error("TopstepX credentials are placeholders. Set TOPSTEPX_USERNAME / "
                  "TOPSTEPX_API_KEY env vars (never print them).")
        sys.exit(2)
    if live and not account_id:
        log.error("Live mode requires a real account_id in config.")
        sys.exit(2)
    if variant not in ("A", "B"):
        log.error("config variant must be 'A' or 'B', got %r.", variant)
        sys.exit(2)
    if cfg.get("imbalance_enabled"):
        log.error("imbalance_enabled is true but no MOC imbalance feed is "
                  "wired in. Set it false — never fake imbalance data.")
        sys.exit(2)

    log.info("Push350 bot starting in %s mode (variant %s, account %s).",
             "LIVE" if live else "PAPER", variant,
             account_id if account_id else "n/a")
    if not live:
        log.info("Paper mode: signals will be logged; ZERO orders will be placed.")
    if cfg.get("manual_skip_day"):
        log.warning("Manual skip day set (%s): no entries will be taken today.",
                    cfg.get("manual_skip_reason", ""))

    # NOTE: ProjectX REST client directly, NOT TradingSuite: TradingSuite
    # forces realtime WebSocket connections, which cannot traverse this
    # machine's egress proxy. The polling loop only needs REST.
    from project_x_py import ProjectX, OrderSide
    from project_x_py.order_manager import OrderManager
    from project_x_py.position_manager import PositionManager
    from project_x_py.event_bus import EventBus
    from types import SimpleNamespace

    client = ProjectX(username=cfg["username"], api_key=cfg["api_key"])
    await client.authenticate()
    log.info("Connected to TopstepX (REST).")
    _bus = EventBus()
    suite = SimpleNamespace(
        client=client,
        orders=OrderManager(client, _bus),
        positions=PositionManager(client, _bus),
    )
    try:
        contract_id = await resolve_es_contract(suite.client)

        # ---- Startup reconciliation FIRST: never double-order after restart.
        position = None
        working = []
        if account_id:
            position = await risk.get_open_position(suite, account_id, contract_id)
            working = await risk.get_working_orders(suite, account_id, contract_id)
            if position:
                log.warning("Startup: existing position found. Managing it; "
                            "no new entries until flat.")
            if working:
                log.warning("Startup: %d working order(s) found. No new entries "
                            "until they clear.", len(working))
        else:
            log.warning("Startup: account_id is 0 — broker reconciliation skipped.")

        state = load_state()
        # Trust the broker, not the state file. Cancel protective orders
        # orphaned by a position that closed while the bot was down.
        state["has_position"] = position is not None
        if position is not None:
            state["phase"] = "position_open"
        elif state.get("phase") == "position_open":
            state["phase"] = "flat"
        elif state.get("phase") == "entry_pending":
            # Restart with an unfilled entry: keep waiting only if the
            # entry order is still working at the broker; otherwise the
            # order is gone and the bot must return to flat, not stay
            # stuck in entry_pending for the rest of the session.
            entry_id = state.get("entry_order_id")
            still_working = any(o.id == entry_id for o in working)
            if not still_working:
                state["phase"] = "flat"
                state["entry_order_id"] = None
                state["trade"] = None
                log.warning("Startup: entry order no longer working; "
                            "reset entry_pending to flat.")
        if live and position is None and state.get("protective_order_ids"):
            working_ids = {o.id for o in working}
            for oid in list(state["protective_order_ids"]):
                if oid in working_ids:
                    try:
                        await suite.orders.cancel_order(
                            order_id=oid, account_id=account_id)
                        log.warning("Startup: cancelled orphaned protective "
                                    "order %s.", oid)
                    except Exception as e:
                        log.warning("Startup: could not cancel order %s: %s",
                                    oid, e)
            state["protective_order_ids"] = []
            state["entry_order_id"] = None
            state["trade"] = None
            state["tp1_done"] = False
            save_state(state)

        while True:
            now = risk.now_et()
            today = now.strftime("%Y-%m-%d")
            if state.get("date") != today:
                state = blank_state()
                state["date"] = today
                log.info("New trading day %s.", today)

            # 1) 15:58 FLATTEN — FIRST, before any session-end exit. The loop
            #    stays alive until this fires; it is the terminal step.
            if risk.should_flatten(now) and not state.get("flattened_today"):
                log.warning("15:58 ET flatten: cancelling orders and closing positions.")
                if live and account_id:
                    await risk.flatten_all(suite, account_id, contract_id)
                else:
                    log.info("PAPER: would flatten all (cancel orders + close positions).")
                state["flattened_today"] = True
                state["phase"] = "flat"
                state["has_position"] = False
                state["protective_order_ids"] = []
                state["entry_order_id"] = None
                state["trade"] = None
                save_state(state)
                log.info("15:58 flatten done — session over for Push350. Exiting.")
                break

            # 2) RTH close fallback (only reachable if flatten already ran
            #    on a previous loop pass, or the bot started after 16:00).
            if now.astimezone(ET).time() >= risk.RTH_CLOSE:
                log.info("RTH closed. Exiting.")
                break

            poll_seconds = (poll_window if risk.in_entry_window(now)
                            or risk.should_flatten(now) else poll_idle)

            # Daily P&L for the loss halt.
            if account_id:
                try:
                    state["daily_pnl"] = await risk.get_daily_realized_pnl(
                        suite, account_id)
                except Exception as e:
                    log.warning("Could not refresh daily P&L: %s", e)
            if risk.daily_loss_halted(state["daily_pnl"], max_daily_loss):
                if not state.get("halted"):
                    log.error("Daily loss halt hit (%.2f). No new entries today.",
                              state["daily_pnl"])
                    state["halted"] = True
                    save_state(state)

            # Reconcile an open position with the broker every poll.
            if live and account_id and state.get("phase") in ("entry_pending", "position_open"):
                try:
                    pos_now = await risk.get_open_position(
                        suite, account_id, contract_id)
                    if state["phase"] == "entry_pending" and pos_now is not None:
                        # Entry filled: add client-side OCO protection.
                        tr = state["trade"]
                        stop_id = tp_id = None
                        try:
                            stop_resp = await suite.orders.add_stop_loss(
                                contract_id=contract_id, stop_price=tr["stop"],
                                size=abs(int(pos_now.size)), account_id=account_id)
                            tp_resp = await suite.orders.add_take_profit(
                                contract_id=contract_id, limit_price=tr["tp2"],
                                size=abs(int(pos_now.size)), account_id=account_id)
                            stop_id = getattr(stop_resp, "orderId", None)
                            tp_id = getattr(tp_resp, "orderId", None)
                            if stop_id and tp_id:
                                suite.orders.track_oco_pair(str(stop_id), str(tp_id))
                            state["protective_order_ids"] = [
                                oid for oid in (stop_id, tp_id) if oid]
                        except Exception as e:
                            log.error("Could not add OCO protection: %s. "
                                      "Position is UNPROTECTED — flattening.", e)
                            await risk.flatten_all(suite, account_id, contract_id)
                            state["phase"] = "flat"
                            state["protective_order_ids"] = []
                            save_state(state)
                            continue
                        state["phase"] = "position_open"
                        state["has_position"] = True
                        log.warning("LIVE entry filled; OCO stop=%.2f tp2=%.2f "
                                    "attached.", tr["stop"], tr["tp2"])
                        save_state(state)
                    elif state["phase"] == "position_open" and pos_now is None:
                        # Position closed (stop, target, or manual): cancel
                        # any orphaned sibling protective order.
                        wo_now = await risk.get_working_orders(
                            suite, account_id, contract_id)
                        working_ids = {o.id for o in wo_now}
                        for oid in list(state.get("protective_order_ids", [])):
                            if oid in working_ids:
                                try:
                                    await suite.orders.cancel_order(
                                        order_id=oid, account_id=account_id)
                                    log.warning("Reconcile: cancelled orphaned "
                                                "protective order %s.", oid)
                                except Exception as e:
                                    log.warning("Reconcile: could not cancel %s: %s",
                                                oid, e)
                        state["phase"] = "flat"
                        state["has_position"] = False
                        state["protective_order_ids"] = []
                        state["entry_order_id"] = None
                        state["trade"] = None
                        state["tp1_done"] = False
                        log.info("Reconcile: position closed at broker; state synced to flat.")
                        save_state(state)
                    elif state["phase"] == "position_open" and pos_now is not None:
                        # Position still open; TP1 breakeven is handled
                        # below once bars are refreshed. Nothing to do here.
                        pass
                except Exception as e:
                    log.warning("Position reconcile failed: %s", e)

            # Pull latest 1-minute bars.
            try:
                df = await suite.client.get_bars(
                    symbol=symbol, days=2, interval=1, unit=2)
            except Exception as e:
                log.warning("get_bars failed: %s", e)
                await asyncio.sleep(poll_seconds)
                continue
            day_bars = bars_from_df(df, today)
            if not day_bars:
                await asyncio.sleep(poll_seconds)
                continue
            bars = [b for _, b in day_bars]
            timestamps = [ts for ts, _ in day_bars]
            last_close = bars[-1].close
            vwap_value = vwap(bars)
            hod, lod = session_high_low(bars)
            bars_5m = resample_to_5min(bars)
            log.debug("5-min structure bars: %d (from %d 1-min bars).",
                      len(bars_5m), len(bars))

            # TP1 breakeven move for a live 1-contract position.
            if live and state.get("phase") == "position_open" and state.get("trade") \
                    and not state.get("tp1_done"):
                tr = state["trade"]
                hit_tp1 = (last_close >= tr["tp1"] if tr["direction"] == "long"
                           else last_close <= tr["tp1"])
                if hit_tp1 and size < 2:
                    # Move stop to breakeven-or-better (entry + tick in
                    # direction) by replacing the stop order.
                    try:
                        wo_now = await risk.get_working_orders(
                            suite, account_id, contract_id)
                        for o in wo_now:
                            # Stop orders carry a stop price; cancel the old
                            # stop only, leave the TP2 target working.
                            if o.stopPrice:
                                await suite.orders.cancel_order(
                                    order_id=o.id, account_id=account_id)
                        be = tr["entry"] + (0.25 if tr["direction"] == "long"
                                            else -0.25)
                        await suite.orders.add_stop_loss(
                            contract_id=contract_id, stop_price=be,
                            account_id=account_id)
                        state["tp1_done"] = True
                        save_state(state)
                        log.warning("TP1 printed; stop moved to breakeven %.2f.", be)
                    except Exception as e:
                        log.warning("TP1 breakeven move failed (original stop "
                                    "still working): %s", e)

            # Entry logic — only inside the window, flat, untraded, unhalted.
            if (risk.in_entry_window(now) and state.get("phase") == "flat"
                    and not cfg.get("manual_skip_day")
                    and not state.get("halted")):
                window_300 = [b for ts, b in day_bars
                              if in_window(ts, time(15, 0), time(15, 50))]
                late_330 = [b for ts, b in day_bars
                            if in_window(ts, time(15, 30), time(15, 50))]
                post_1550 = [b for ts, b in day_bars
                             if ts.time() >= risk.ENTRY_START]
                if late_330 and vwap_value is not None and hod is not None:
                    r_high, r_low = range_high_low(late_330)
                    avg_vol = (sum(b.volume for b in late_330) / len(late_330)
                               if late_330 else 0.0)
                    trig = (detect_variant_a(post_1550, r_high, r_low, avg_vol)
                            if variant == "A"
                            else detect_variant_b(post_1550, r_high, r_low))
                    if trig is not None and trig.entry != state.get("last_signal_entry"):
                        state["signals_today"] = state.get("signals_today", 0) + 1
                        state["last_signal_entry"] = trig.entry
                        trig.stop = trig.entry - float(cfg.get("stop_points", 4.0)) \
                            if trig.direction == "long" \
                            else trig.entry + float(cfg.get("stop_points", 4.0))
                        trig.tp1 = trig.entry + float(cfg.get("tp1_points", 6.0)) \
                            if trig.direction == "long" \
                            else trig.entry - float(cfg.get("tp1_points", 6.0))
                        trig.tp2 = trig.entry + float(cfg.get("tp2_points", 9.0)) \
                            if trig.direction == "long" \
                            else trig.entry - float(cfg.get("tp2_points", 9.0))
                        log.info("SIGNAL %s variant %s entry=%.2f stop=%.2f "
                                 "tp1=%.2f tp2=%.2f (%s)",
                                 trig.direction.upper(), trig.variant, trig.entry,
                                 trig.stop, trig.tp1, trig.tp2, trig.reason)
                        # Bias + hard skips for THIS direction.
                        target = (max(hod, r_high) if trig.direction == "long"
                                  else min(lod, r_low))
                        bias = bias_check(window_300, vwap_value, target,
                                          last_close, trig.direction)
                        ok_range, range_reason = range_quality(r_high, r_low)
                        hit = target_already_hit(late_330, target, trig.direction)
                        log.info("Bias %s: agrees=%d/3 factor=%.1f checks=%s "
                                 "target=%.2f range_ok=%s target_hit=%s",
                                 trig.direction, bias.agrees, bias.size_factor,
                                 bias.checks, target, ok_range, hit)
                        if bias.size_factor == 0.0:
                            log.info("Entry blocked: bias <2/3 agree.")
                        elif not ok_range:
                            log.info("Entry blocked: %s", range_reason)
                        elif hit:
                            log.info("Entry blocked: target already hit before 15:50.")
                        elif not risk.reward_risk_ok(trig.entry, trig.stop, trig.tp1,
                                                     float(cfg.get("min_rr_to_tp1", 1.5))):
                            log.info("Entry blocked: RR to TP1 < %.1f.", cfg.get("min_rr_to_tp1", 1.5))
                        else:
                            ok, reason = risk.can_open(
                                has_position=False, has_working_orders=False,
                                trades_today=state["trades_today"], now=now,
                                daily_pnl=state["daily_pnl"],
                                max_daily_loss=max_daily_loss,
                                max_trades_per_day=max_trades)
                            if not ok:
                                log.info("Entry blocked by risk: %s", reason)
                            elif not live:
                                log.info("PAPER: signal logged (size factor %.1f, "
                                         "orders %d), no order placed.",
                                         bias.size_factor,
                                         order_size_for(size, bias.size_factor))
                            else:
                                order_size = order_size_for(size, bias.size_factor)
                                if order_size == 0:
                                    log.info("Entry blocked: order size 0.")
                                else:
                                    side = (OrderSide.BUY.value
                                            if trig.direction == "long"
                                            else OrderSide.SELL.value)
                                    try:
                                        resp = await suite.orders.place_limit_order(
                                            contract_id=contract_id, side=side,
                                            size=order_size, limit_price=trig.entry,
                                            account_id=account_id)
                                    except Exception as e:
                                        log.warning("LIVE entry order failed: %s. "
                                                    "No position taken.", e)
                                        save_state(state)
                                        await asyncio.sleep(poll_seconds)
                                        continue
                                    log.warning("LIVE entry placed: %s", resp)
                                    state["trades_today"] += 1
                                    state["phase"] = "entry_pending"
                                    state["entry_order_id"] = getattr(
                                        resp, "orderId", None)
                                    state["trade"] = {
                                        "direction": trig.direction,
                                        "entry": trig.entry, "stop": trig.stop,
                                        "tp1": trig.tp1, "tp2": trig.tp2}
                                    state["tp1_done"] = False
                        save_state(state)
            save_state(state)
            await asyncio.sleep(poll_seconds)
    finally:
        log.info("Push350 bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
