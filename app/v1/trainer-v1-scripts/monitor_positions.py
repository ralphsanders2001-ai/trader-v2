"""
Position monitor: check open trades, auto-take profit, block loss exits until approved.

Uses robin_stocks for option prices (same data source as live monitor).
Monitors ONLY active trades (status='open') in the paper DB.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz
from datetime import datetime

import robin_stocks as r
from database import init_db, get_db
from execution import ExecutionEngine
from account import AccountManager

import config
from rh_credentials import ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD

_PAPER_SESSION = None

def _login_paper():
    """Authenticate to Robinhood for paper price lookups (paper doesn't trade, just reads prices)."""
    global _PAPER_SESSION
    if _PAPER_SESSION:
        return True
    r.robinhood.login(
        username=ROBINHOOD_USERNAME,
        password=ROBINHOOD_PASSWORD,
        store_session=True,
        pickle_path=config.ROBINHOOD_PICKLE_PATH,
    )
    _PAPER_SESSION = True
    return True


def _get_robinhood_option_price(symbol, expiry, strike, option_type):
    """Fetch the current mid-price of an option from Robinhood.
    Returns (mark, bid, ask) tuple; mark is 0 if no quote available.
    """
    try:
        opts = r.robinhood.find_options_by_expiration_and_strike(
            symbol, expirationDate=expiry, strikePrice=strike, optionType=option_type
        )
        if opts:
            o = opts[0]
            bid = float(o.get('bid_price') or 0)
            ask = float(o.get('ask_price') or 0)
            mark = (bid + ask) / 2 if bid > 0 and ask > 0 else float(o.get('mark_price') or 0)
            return mark, bid, ask
    except Exception:
        pass
    return 0, 0, 0


def _get_robinhood_underlying_price(symbol):
    """Get the latest underlying price from Robinhood."""
    try:
        quote = r.robinhood.get_latest_price(symbol)
        if quote and len(quote) > 0:
            return float(quote[0])
    except Exception:
        pass
    return 0.0


def _minutes_since(ts: str) -> float:
    """Minutes since ISO timestamp. Mirrors monitor_active.py logic."""
    if not ts:
        return 0.0
    try:
        normalized = ts.replace("Z", "+00:00")
        entry = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            t = datetime.strptime(ts, "%H:%M:%S").time()
            et = pytz.timezone(config.MARKET_TIMEZONE)
            entry = et.localize(datetime.combine(datetime.now(et).date(), t))
        except Exception:
            return 0.0
    if entry.tzinfo is None:
        et = pytz.timezone(config.MARKET_TIMEZONE)
        entry = et.localize(entry)
    now = datetime.now(pytz.UTC)
    return (now - entry).total_seconds() / 60.0


def _is_market_open() -> bool:
    tz = pytz.timezone(config.MARKET_TIMEZONE)
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    open_mins = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MINUTE
    close_mins = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MINUTE
    time_mins = now.hour * 60 + now.minute
    return open_mins <= time_mins < close_mins


def monitor_positions(mode: str = None):
    """Iterate over open trades and apply the same rules as the live monitor.

    Monitors ONLY active trades (status='open'). Uses robin_stocks for prices.
    """
    init_db()
    mode = mode or "paper"

    if not _is_market_open():
        print(f"[{mode.upper()} MONITOR] Outside market hours, skipping")
        return

    _login_paper()

    # Heartbeat
    tz = pytz.timezone(config.MARKET_TIMEZONE)
    now = datetime.now(tz)
    now_utc = datetime.now(pytz.UTC)
    print(f"\n[HEARTBEAT] system_utc={now_utc.isoformat()} et={now.isoformat()}")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE mode=? AND status='open' ORDER BY entry_time",
            (mode,)
        ).fetchall()

    if not rows:
        print(f"[{mode.upper()} MONITOR] No open positions")
        return

    engine = ExecutionEngine(mode=mode)
    account = AccountManager()
    account.check_and_reset_counts(mode=mode)

    after_cutoff = (now.hour > config.AUTO_LOSS_CUTOFF_HOUR) or (
        now.hour == config.AUTO_LOSS_CUTOFF_HOUR and now.minute >= config.AUTO_LOSS_CUTOFF_MINUTE
    )

    print(f"\n[{mode.upper()} MONITOR] {now.strftime('%I:%M %p %Z')} — {len(rows)} open position(s)")
    print(f"  Rules: +{config.AUTO_PROFIT_PCT:.0%} profit | -{config.DEEP_LOSS_PCT:.0%} deep loss | {config.MAX_HOLD_MINUTES}min max hold | auto-exit losses after {config.AUTO_LOSS_CUTOFF_HOUR:02d}:{config.AUTO_LOSS_CUTOFF_MINUTE:02d} ET")

    for row in rows:
        trade = dict(row)
        symbol = trade["symbol"]
        option_type = trade["option_type"]
        strike = trade["option_strike"]
        expiry = trade["option_expiry"]
        entry = trade["entry_price"]
        qty = trade["quantity"]
        trade_id = trade["id"]

        # Current price from Robinhood (same source as live monitor)
        current, bid, ask = _get_robinhood_option_price(symbol, expiry, strike, option_type)
        if current <= 0:
            print(f"  {symbol} {option_type}{strike}: no market data, skipping")
            continue

        underlying = _get_robinhood_underlying_price(symbol)

        unrealized = (current - entry) * qty * 100
        pct = (current - entry) / entry if entry > 0 else 0
        mins = _minutes_since(trade["entry_time"])

        print(f"  {symbol} {option_type} strike={strike} exp={expiry}")
        print(f"    entry={entry:.2f} curr={current:.2f} bid={bid:.2f} ask={ask:.2f} ({pct:+.0%}) held={mins:.0f}min unrealized={unrealized:+.2f} underlying={underlying:.2f}")

        # Same 4 rules as live monitor
        # Rule 1: profit target
        if pct >= config.AUTO_PROFIT_PCT:
            print(f"    >> PROFIT TARGET (+{pct:.0%}) — closing")
            res = engine.close(trade_id, current, approve_loss=False)
            print(f"    >> {res.message}")
        # Rule 2: deep loss
        elif pct <= -config.DEEP_LOSS_PCT:
            print(f"    >> DEEP LOSS ({pct:.0%}) — cutting")
            res = engine.close(trade_id, current, approve_loss=True)
            print(f"    >> {res.message}")
        # Rule 3: max hold (only if not in profit)
        elif mins >= config.MAX_HOLD_MINUTES:
            if unrealized <= 0:
                print(f"    >> MAX HOLD ({mins:.0f}min, not in profit) — closing")
                res = engine.close(trade_id, current, approve_loss=True)
                print(f"    >> {res.message}")
            else:
                print(f"    >> MAX HOLD ({mins:.0f}min, in profit but below {config.AUTO_PROFIT_PCT:.0%}) — holding")
        # Rule 4: end-of-day loss exit
        elif after_cutoff and unrealized < 0:
            print(f"    >> AFTER {config.AUTO_LOSS_CUTOFF_HOUR:02d}:{config.AUTO_LOSS_CUTOFF_MINUTE:02d} LOSS — closing at {current:.2f}")
            res = engine.close(trade_id, current, approve_loss=True)
            print(f"    >> {res.message}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default=None)
    args = parser.parse_args()
    monitor_positions(args.mode)
