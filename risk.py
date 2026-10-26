"""Risk guards for the Push350 bot — pure checks plus async broker helpers.

Pure functions take explicit arguments and are unit-testable. The async
helpers query the TopstepX account through the project-x-py REST client
(no realtime websockets — same pattern as the S4 bot).

CRITICAL ORDERING (the S4 latent bug, fixed here by construction):
the 15:58 flatten check must sit ABOVE any RTH-close exit in bot.py's
loop. This bot flattens at 15:58 ET — BEFORE the 16:00 RTH close — so
the flatten can never be skipped by an earlier exit. After the 15:58
flatten the session is over for this bot and the loop exits; there is
no overnight position, ever.
"""

from datetime import datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)     # regular trading hours start (ET)
RTH_CLOSE = time(16, 0)    # regular trading hours end (ET)
ENTRY_START = time(15, 50)  # entry window opens (ET)
ENTRY_END = time(15, 55)    # last entry minute (ET) — NO entry at 15:56+
FLATTEN_TIME = time(15, 58)  # flat or breakeven-or-better by 15:58 ET
MAX_TRADES_PER_DAY = 1
MAX_DAILY_LOSS = 500.0
MIN_RR_TO_TP1 = 1.5


# ---------------------------------------------------------------------------
# Pure guards
# ---------------------------------------------------------------------------

def now_et() -> datetime:
    return datetime.now(ET)


def _time_of(now: datetime) -> time:
    return now.astimezone(ET).time() if now.tzinfo else now.time()


def in_entry_window(now: Optional[datetime] = None) -> bool:
    """True during the 15:50-15:55 ET entry window (15:56 is too late)."""
    now = now or now_et()
    cur = _time_of(now)
    return ENTRY_START <= cur <= ENTRY_END


def should_flatten(now: Optional[datetime] = None) -> bool:
    """True at/after 15:58 ET — flatten everything, no overnight positions."""
    now = now or now_et()
    return _time_of(now) >= FLATTEN_TIME


def has_time_to_tp(now: Optional[datetime] = None,
                   min_minutes: float = 1.0) -> bool:
    """True if at least min_minutes remain before the 15:58 time stop.

    An entry with less than a minute to the flatten cannot plausibly
    reach TP1 first, so it is skipped even if it is inside the window.
    """
    now = now or now_et()
    now_et_dt = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    flatten_dt = now_et_dt.replace(hour=FLATTEN_TIME.hour,
                                   minute=FLATTEN_TIME.minute,
                                   second=0, microsecond=0)
    return (flatten_dt - now_et_dt) >= timedelta(minutes=min_minutes)


def daily_loss_halted(daily_pnl: float, max_daily_loss: float = MAX_DAILY_LOSS) -> bool:
    """Halt new entries once realized daily P&L hits -max_daily_loss."""
    return daily_pnl <= -abs(max_daily_loss)


def reward_risk_ok(entry: float, stop: float, tp1: float,
                   min_rr: float = MIN_RR_TO_TP1) -> bool:
    """Require >= min_rr reward/risk to TP1, either direction."""
    risk = abs(entry - stop)
    if risk == 0:
        return False
    return abs(tp1 - entry) / risk >= min_rr


def can_open(has_position: bool,
             has_working_orders: bool,
             trades_today: int,
             now: Optional[datetime] = None,
             daily_pnl: float = 0.0,
             max_daily_loss: float = MAX_DAILY_LOSS,
             max_trades_per_day: int = MAX_TRADES_PER_DAY) -> Tuple[bool, str]:
    """Gate for a new entry. Returns (ok, reason). Order matters: the
    flatten check comes first so a post-15:58 call can never open."""
    now = now or now_et()
    if should_flatten(now):
        return False, "past flatten time (15:58 ET)"
    if not in_entry_window(now):
        return False, "outside entry window (15:50-15:55 ET)"
    if not has_time_to_tp(now):
        return False, "no time to TP1 before 15:58 time stop"
    if has_position:
        return False, "position already open (max 1)"
    if has_working_orders:
        return False, "working orders present"
    if trades_today >= max_trades_per_day:
        return False, f"already traded today ({trades_today}/{max_trades_per_day})"
    if daily_loss_halted(daily_pnl, max_daily_loss):
        return False, f"daily loss halt hit ({daily_pnl:.2f} <= -{max_daily_loss:.2f})"
    return True, "ok"


# ---------------------------------------------------------------------------
# Async broker helpers (require a connected ProjectX REST client)
# ---------------------------------------------------------------------------

async def get_open_position(suite, account_id: int, contract_id: str):
    """Return the open position for our contract, or None."""
    positions = await suite.positions.get_all_positions(account_id=account_id)
    for p in positions:
        if p.contractId == contract_id and p.size != 0:
            return p
    return None


async def get_working_orders(suite, account_id: int, contract_id: str):
    """Return working (non-terminal) orders for our contract."""
    working_statuses = {1, 6, 7, 8}  # OPEN, PENDING, PENDING_CANCELLATION, SUSPENDED
    orders = await suite.orders.search_open_orders(
        contract_id=contract_id, account_id=account_id)
    return [o for o in orders
            if o.contractId == contract_id and int(o.status) in working_statuses]


async def flatten_all(suite, account_id: int, contract_id: str) -> None:
    """Cancel all working orders and close all positions. Idempotent."""
    await suite.orders.cancel_all_orders(
        contract_id=contract_id, account_id=account_id)
    await suite.positions.close_all_positions(
        contract_id=contract_id, account_id=account_id)


async def get_daily_realized_pnl(suite, account_id: int) -> float:
    """Net realized P&L for today (ET) from the trade history."""
    start = now_et().replace(hour=0, minute=0, second=0, microsecond=0)
    trades = await suite.client.search_trades(
        start_date=start, account_id=account_id, limit=500)
    pnl = 0.0
    for t in trades:
        if t.voided:
            continue
        pnl += (t.profitAndLoss or 0.0) - (t.fees or 0.0) - (t.commissions or 0.0)
    return pnl
