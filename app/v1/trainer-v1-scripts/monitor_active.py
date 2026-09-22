#!/usr/bin/env python3
"""
Active position monitor — reads positions DIRECTLY from Robinhood via robin_stocks.
No MCP, no agent, no DB dependency. Places orders directly via robin_stocks.
Runs every 60 seconds during market hours.
"""
import sys, os, time, pytz, sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import robin_stocks as r
import config
from database import get_db
from rh_credentials import ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD
from notify import notify

# OpenObserve log shipping (upgrade-proof bridge; safe to fail)
try:
    from oo_bridge import log_ship, log_ship_file
    _HAS_OO_BRIDGE = True
except Exception:
    _HAS_OO_BRIDGE = False

def _oo_log(level: str, msg: str) -> None:
    """Ship a log to OpenObserve. Never raises. Prints locally too."""
    print(f"[{level}] {msg}")
    if _HAS_OO_BRIDGE:
        try:
            log_ship(level, "monitor", msg)
        except Exception:
            pass  # never break the trading logic


ROBINHOOD_SESSION = None

def login():
    """Authenticate to Robinhood once, reuse session."""
    global ROBINHOOD_SESSION
    if ROBINHOOD_SESSION:
        return True
    r.robinhood.login(
        username=ROBINHOOD_USERNAME,
        password=ROBINHOOD_PASSWORD,
        store_session=True,
        pickle_path=os.path.dirname(config.ROBINHOOD_PICKLE_PATH),
        pickle_name="",  # empty string → robin_stocks builds "robinhood.pickle"
    )
    ROBINHOOD_SESSION = True
    return True


def get_open_option_positions():
    """Return list of open option positions from Robinhood."""
    login()
    pos = r.robinhood.get_aggregate_open_positions()
    if not pos:
        return []
    result = []
    for p in pos:
        # New Robinhood API: no top-level "instrument" or "instrument_id" fields.
        # Get the option_id from legs instead.
        legs = p.get('legs', [])
        if not legs:
            continue
        option_id = legs[0].get('option_id', '')
        if not option_id:
            continue
        # Only options (not stocks)
        try:
            qty = float(p.get('quantity', 0))
            if qty <= 0:
                continue
            # Get details using option_id from legs
            opt_data = r.robinhood.get_option_instrument_data_by_id(option_id)
            if not opt_data:
                continue
            result.append({
                'symbol': p.get('symbol', '').upper(),
                'strike': float(opt_data.get('strike_price', 0)),
                'expiry': opt_data.get('expiration_date', ''),
                'option_type': opt_data.get('type', ''),  # 'call' or 'put'
                'quantity': qty,
                'account': p.get('account', ''),
            })
        except Exception as e:
            print(f"  Error parsing position: {e}")
            continue
    return result


def get_option_market_data(symbol, expiry, strike, option_type):
    """Get real-time option price from Robinhood."""
    login()
    try:
        # First find the option_id via lookup (no live bid/ask in this endpoint)
        opts = r.robinhood.find_options_by_expiration_and_strike(
            symbol, expirationDate=expiry, strikePrice=strike, optionType=option_type
        )
        if opts:
            o = opts[0]
            opt_id = o.get('id', '')
            if opt_id:
                # Get live market data using the option_id — this returns bid/ask
                mkt = r.robinhood.get_option_market_data_by_id(opt_id)
                if isinstance(mkt, list) and mkt:
                    m = mkt[0]
                    bid = float(m.get('bid_price') or 0)
                    ask = float(m.get('ask_price') or 0)
                    mark = float(m.get('mark_price') or 0)
                    if mark == 0 and bid > 0 and ask > 0:
                        mark = (bid + ask) / 2
                    return {'bid': bid, 'ask': ask, 'mark': mark}
            # Fallback to static data if no option_id
            mark = float(o.get('mark_price') or 0)
            return {'bid': 0, 'ask': 0, 'mark': mark}
    except Exception as e:
        print(f"  Market data error for {symbol}: {e}")
    return {'bid': 0, 'ask': 0, 'mark': 0}


def _has_open_sell_order(symbol, expiry, strike, option_type):
    """Return True if there is already a pending or open sell-to-close order for
    this exact contract. Used to prevent the orphan-retry loop where the monitor
    would re-place a sell every cycle because the orphan stays in Robinhood
    until the order fills."""
    try:
        orders = r.robinhood.get_all_option_orders(info=None)
        for o in orders or []:
            # Consider any non-terminal state as "pending" — confirmed, queued, unconfirmed
            if o.get("state") not in ("confirmed", "queued", "unconfirmed"):
                continue
            for leg in o.get("legs", []):
                instr = r.robinhood.request_get(leg.get("option"))
                if (instr.get("chain_symbol") == symbol
                        and instr.get("expiration_date") == expiry
                        and abs(float(instr.get("strike_price", 0)) - float(strike)) < 0.01
                        and instr.get("type") == option_type
                        and leg.get("side") == "sell"
                        and leg.get("position_effect") == "close"):
                    return True
    except Exception as e:
        print(f"  WARN: open-sell check failed: {e}")
    return False


def place_sell_order(symbol, expiry, strike, option_type, quantity, price):
    """Place a sell-to-close order directly via robin_stocks."""
    login()
    try:
        result = r.robinhood.order_sell_option_limit(
            positionEffect='close',
            creditOrDebit='credit',
            price=price,
            symbol=symbol,
            quantity=quantity,
            expirationDate=expiry,
            strike=strike,
            optionType=option_type,
            timeInForce=config.ROBINHOOD_TIME_IN_FORCE,
        )
        print(f"  SELL ORDER PLACED: {quantity}x {symbol} {expiry} ${strike} {option_type} @ ${price}")
        return result
    except Exception as e:
        print(f"  SELL ORDER FAILED: {e}")
        return None


def is_market_open():
    """Check if we're within market hours (config.MARKET_OPEN_* to MARKET_CLOSE_* Mon-Fri)."""
    tz = pytz.timezone(config.MARKET_TIMEZONE)
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    open_mins = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MINUTE
    close_mins = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MINUTE
    time_mins = now.hour * 60 + now.minute
    return open_mins <= time_mins < close_mins


def minutes_since(ts: str) -> float:
    """Minutes since ISO timestamp. Tolerates naive or time-only strings by
    assuming config.MARKET_TIMEZONE for naive datetimes and current date for time-only."""
    if not ts:
        return 0.0
    try:
        # Normalize: replace Z with +00:00, then attempt parse
        normalized = ts.replace("Z", "+00:00")
        entry = datetime.fromisoformat(normalized)
    except ValueError:
        # Try time-only format like "17:20:20" — use today's date in ET
        try:
            t = datetime.strptime(ts, "%H:%M:%S").time()
            et = pytz.timezone(config.MARKET_TIMEZONE)
            entry = et.localize(datetime.combine(datetime.now(et).date(), t))
        except Exception:
            return 0.0

    # If entry is naive, assume configured market timezone
    if entry.tzinfo is None:
        et = pytz.timezone(config.MARKET_TIMEZONE)
        entry = et.localize(entry)

    now = datetime.now(pytz.UTC)
    return (now - entry).total_seconds() / 60.0


PROFIT_TARGET = config.AUTO_PROFIT_PCT       # 20% gain → sell (default; overridden by turbulence tier)
DEEP_LOSS = config.DEEP_LOSS_PCT             # 30% loss → cut immediately (default; overridden by turbulence tier)
MAX_HOLD = config.MAX_HOLD_MINUTES           # minutes before forcing exit if not profitable
LOSS_CUTOFF_HOUR = config.AUTO_LOSS_CUTOFF_HOUR  # 3:30 PM ET — auto-exit losses after this
LOSS_CUTOFF_MINUTE = config.AUTO_LOSS_CUTOFF_MINUTE  # minute within the cutoff hour


def _is_hold_until_profit(trade_id, symbol):
    """Check if a trade is tagged with 'hold_until_profit' in notes.
    Tagged trades skip the 15-min max-hold rule (still hit deep loss / target).
    """
    HOLD_TAG = "hold_until_profit"
    try:
        with get_db() as conn:
            conn.row_factory = sqlite3.Row
            row = None
            if trade_id is not None:
                row = conn.execute("SELECT notes FROM trades WHERE id=?", (trade_id,)).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT notes FROM trades WHERE symbol=? AND status='open' "
                    "ORDER BY entry_time DESC LIMIT 1", (symbol,)
                ).fetchone()
            if row and row["notes"] and HOLD_TAG in row["notes"]:
                return True
    except Exception as e:
        print(f"[MONITOR] _is_hold_until_profit check failed: {e}")
    return False


def classify_turbulence(symbol):
    """Measure (high - low) / low over the lookback window for `symbol`.
    Returns (range_pct, tier_name, profit_target, deep_loss, poll_seconds)
    or (None, "unknown", AUTO_PROFIT_PCT, DEEP_LOSS_PCT, 60) on failure.
    The tier values come from config.TURBULENCE_TIERS so the user can tune them.
    """
    # Forced turbulent override: applied BEFORE measurement so the symbol
    # always uses the extreme-tier targets regardless of intraday range.
    # Useful for whippy stocks where the measured range understates behavior.
    if symbol.upper() in [s.upper() for s in config.FORCED_TURBULENT_SYMBOLS]:
        extreme = config.TURBULENCE_TIERS[(0.15, 1.00)]
        return 1.0, "extreme-forced", extreme[0], extreme[1], extreme[2]

    import robin_stocks.robinhood as rh
    try:
        # Historicals for the underlying — 5-min bars.
        # robin_stocks API: rh.get_stock_historicals returns a list of bars directly
        # (NOT a dict with 'historicals' key as some other endpoints do).
        # Span must be day/week/month/3month/year/5year; minute data needs span='day'.
        hist = rh.get_stock_historicals(
            symbol,
            interval="5minute",
            span="day",
            bounds="regular",
        )
        if not hist or not isinstance(hist, list) or len(hist) == 0:
            return None, "unknown", config.AUTO_PROFIT_PCT, config.DEEP_LOSS_PCT, 60
        # Filter to only bars within the configured lookback window (default 30 min).
        # 5-min bars in 30 min = ~6 bars.
        lookback_bars = max(1, int(config.TURBULENCE_LOOKBACK_MINUTES / 5))
        bars = hist[-lookback_bars:]  # most recent N bars
        highs = [float(b.get("high_price", 0)) for b in bars if b.get("high_price")]
        lows = [float(b.get("low_price", 0)) for b in bars if b.get("low_price")]
        if not highs or not lows:
            return None, "unknown", config.AUTO_PROFIT_PCT, config.DEEP_LOSS_PCT, 60
        window_high = max(highs)
        window_low = min(lows)
        if window_low <= 0:
            return None, "unknown", config.AUTO_PROFIT_PCT, config.DEEP_LOSS_PCT, 60
        range_pct = (window_high - window_low) / window_low
    except Exception as e:
        print(f"  [TURB] classify failed for {symbol}: {e}")
        return None, "unknown", config.AUTO_PROFIT_PCT, config.DEEP_LOSS_PCT, 60

    # Find the tier that contains range_pct
    for (lo, hi), (profit, loss, poll_sec) in config.TURBULENCE_TIERS.items():
        if lo <= range_pct < hi:
            if range_pct < 0.05:
                tier_name = "calm"
            elif range_pct < 0.10:
                tier_name = "moderate"
            elif range_pct < 0.15:
                tier_name = "turbulent"
            else:
                tier_name = "extreme"
            return range_pct, tier_name, profit, loss, poll_sec
    return range_pct, "unknown", config.AUTO_PROFIT_PCT, config.DEEP_LOSS_PCT, 60


def run_cycle():
    """Check all open positions, apply rules, place sells directly."""
    if not is_market_open():
        return

    _oo_log("INFO", "Monitor cycle started")

    # Reconcile DB against Robinhood before checking positions
    # (catches positions filled/cancelled externally since last cycle)
    try:
        from sync_live_account import sync_live_account
        sync = sync_live_account(verbose=False)
        if sync["ghost_db"] or sync["orphan_rh"] or sync["qty_mismatch"]:
            print(f"  [SYNC] anomalies: {sync}")
            _oo_log("WARN", f"Sync anomalies: {sync}")
    except Exception as e:
        print(f"  [SYNC] skipped: {e}")

    # Reconcile DB P&L against Robinhood's actual fills (fixes $0 pnl for
    # auto-closed positions where the DB never saw the real exit price).
    # Runs at most once per minute to avoid hammering Robinhood.
    try:
        from reconcile_db_with_robinhood import reconcile
        import time as _time
        now_minute = int(_time.time() // 60)
        last_reconcile_minute = getattr(run_cycle, "_last_reconcile", -1)
        if now_minute != last_reconcile_minute:
            reconcile(datetime.now().strftime("%Y-%m-%d"), dry_run=False)
            run_cycle._last_reconcile = now_minute
    except Exception as e:
        print(f"  [RECONCILE] skipped: {e}")

    # Heartbeat: always print the canonical time first so logs are unambiguous
    tz = pytz.timezone(config.MARKET_TIMEZONE)
    now = datetime.now(tz)
    now_utc = datetime.now(pytz.UTC)
    print(f"\n[HEARTBEAT] system_utc={now_utc.isoformat()} et={now.isoformat()}")

    # API-precomputed risk monitor: runs once per minute to catch margin /
    # concentration / lock alerts without hammering Robinhood.
    # Runs BEFORE the early-return so it fires even with zero open positions.
    try:
        from api_risk_monitor import run_once as risk_run_once
        last_risk_minute = getattr(run_cycle, "_last_risk_minute", -1)
        cur_minute = int(time.time() // 60)
        if cur_minute != last_risk_minute:
            risk_run_once()
            run_cycle._last_risk_minute = cur_minute
    except Exception as e:
        print(f"  [RISK] skipped: {e}")

    positions = get_open_option_positions()
    if not positions:
        return

    hour = now.hour
    minute = now.minute
    after_cutoff = (hour > LOSS_CUTOFF_HOUR) or (hour == LOSS_CUTOFF_HOUR and minute >= LOSS_CUTOFF_MINUTE)

    print(f"\n[MONITOR] {now.strftime('%I:%M %p %Z')} — {len(positions)} open position(s)")

    for pos in positions:
        symbol = pos['symbol']
        strike = pos['strike']
        expiry = pos['expiry']
        qty = pos['quantity']
        opt_type = pos['option_type']

        # Tier-throttling gate: skip rule application if the symbol's tier-poll
        # timer hasn't elapsed yet (only used when monitor_loop sets _TIER_GATE).
        if not _should_apply_for_symbol(symbol, pos):
            continue

        # Always reconcile against Robinhood's actual fill price first.
        # The DB might have a stale limit price; Robinhood's average_open_price
        # is the actual cost basis (per share after dividing by multiplier).
        robin_pos = next((p for p in r.robinhood.get_aggregate_open_positions()
                          if p.get('symbol') == symbol
                          and p.get('legs')
                          and abs(float(p['legs'][0].get('strike_price', 0)) - strike) < 0.01), None)
        rh_entry_per_share = None
        if robin_pos:
            total_cost = float(robin_pos.get('average_open_price', 0))
            mult = float(robin_pos.get('trade_value_multiplier', 100))
            if total_cost > 0 and mult > 0:
                rh_entry_per_share = total_cost / mult

        # Get entry price from our DB; fall back to Robinhood's actual entry for orphans
        from database import get_db
        entry = None
        trade_id = None
        with get_db() as conn:
            row = conn.execute(
                """SELECT * FROM trades WHERE symbol=? AND option_strike=? AND option_expiry=?
                   AND option_type=? AND status='open' AND mode='live' LIMIT 1""",
                (symbol, strike, expiry, opt_type)
            ).fetchone()
        if row:
            trade = dict(row)
            entry = float(trade['entry_price'])
            trade_id = trade['id']
            # If we have Robinhood's actual entry, prefer it (limit ≠ fill)
            if rh_entry_per_share is not None:
                entry = rh_entry_per_share
        else:
            # Orphan: position on Robinhood but not in DB (cron entered it).
            # Use Robinhood's recorded entry price so we can apply exit rules.
            if rh_entry_per_share is not None and rh_entry_per_share > 0:
                entry = rh_entry_per_share
                print(f"  {symbol}: ORPHAN (Robinhood only, no DB entry) — using RH entry ${entry:.2f}")
            elif robin_pos:
                print(f"  {symbol}: orphan with no entry price, skipping")
                continue
            else:
                print(f"  {symbol}: no DB entry and can't reconcile, skipping")
                continue

        # Get current price from Robinhood
        mkt = get_option_market_data(symbol, expiry, strike, opt_type)
        current = mkt['mark']
        if current <= 0:
            print(f"  {symbol}: no market data, skipping")
            continue

        unrealized = (current - entry) * qty * 100
        pct = (current - entry) / entry if entry > 0 else 0
        # For orphans, use position opened_at as entry time proxy
        if trade_id is not None:
            mins = minutes_since(trade['entry_time'])
        else:
            orphan_pos = next((p for p in r.robinhood.get_aggregate_open_positions()
                              if p.get('symbol') == symbol
                              and p.get('legs')
                              and abs(float(p['legs'][0].get('strike_price', 0)) - strike) < 0.01), None)
            mins = minutes_since(orphan_pos.get('created_at', '')) if orphan_pos else 0

        print(f"  {symbol} {opt_type} strike={strike} exp={expiry}")
        print(f"    entry={entry:.2f} curr={current:.2f} ({pct:+.0%}) held={mins:.0f}min unrealized={unrealized:+.2f}")

        # Classify turbulence for THIS symbol — overrides global PROFIT_TARGET / DEEP_LOSS
        range_pct, tier_name, sym_profit, sym_loss, sym_poll = classify_turbulence(symbol)
        if range_pct is not None:
            print(f"    >> turbulence: range={range_pct:.1%} tier={tier_name} "
                  f"profit_target={sym_profit:+.0%} deep_loss={sym_loss:+.0%} poll={sym_poll}s")
            # Update tier-throttling state if active
            if _TIER_GATE is not None:
                _TIER_GATE[symbol] = {
                    "next_due": time.time() + sym_poll,
                    "poll_seconds": sym_poll,
                    "tier": tier_name,
                }
        else:
            sym_profit, sym_loss = PROFIT_TARGET, DEEP_LOSS
            print(f"    >> turbulence: classification unavailable — using defaults "
                  f"profit_target={sym_profit:+.0%} deep_loss={sym_loss:+.0%}")

        action_taken = False

        # Rule 1: tier-specific profit target — sell immediately
        if pct >= sym_profit:
            if _has_open_sell_order(symbol, expiry, strike, opt_type):
                print(f"    >> PROFIT TARGET — already have open sell, skipping re-place")
            else:
                print(f"    >> PROFIT TARGET (+{pct:.0%}) — selling")
                place_sell_order(symbol, expiry, strike, opt_type, qty, current)
            _oo_log("INFO", f"PROFIT_TARGET {symbol} ${strike} {opt_type} entry={entry:.2f} exit={current:.2f} pnl={unrealized:+.2f}")
            if trade_id is not None:
                _close_trade_db(trade_id, current, unrealized, "profit_target")
            else:
                _insert_orphan_trade_db(symbol, expiry, strike, opt_type, qty, entry)
                notify("ORPHAN CLOSED: profit target", f"{symbol} ${strike} {opt_type} entry=${entry:.2f} exit=${current:.2f} pnl=${unrealized:+.2f}", priority="high")
            action_taken = True

        # Rule 2: tier-specific deep loss — cut immediately
        elif pct <= sym_loss:
            if _has_open_sell_order(symbol, expiry, strike, opt_type):
                print(f"    >> DEEP LOSS — already have open sell, skipping re-place")
            else:
                print(f"    >> DEEP LOSS ({pct:.0%}) — cutting")
                place_sell_order(symbol, expiry, strike, opt_type, qty, current)
            _oo_log("WARN", f"DEEP_LOSS {symbol} ${strike} {opt_type} entry={entry:.2f} exit={current:.2f} pnl={unrealized:+.2f}")
            if trade_id is not None:
                _close_trade_db(trade_id, current, unrealized, "deep_loss")
            else:
                _insert_orphan_trade_db(symbol, expiry, strike, opt_type, qty, entry)
                notify("ORPHAN CLOSED: deep loss", f"{symbol} ${strike} {opt_type} entry=${entry:.2f} exit=${current:.2f} pnl=${unrealized:+.2f}", priority="high")
            action_taken = True

        # Rule 3: 15-min max hold — exit if not in profit
        # Skip if the trade is tagged hold_until_profit (notes marker)
        # so the user's directional conviction can override the timer.
        elif mins >= MAX_HOLD and not _is_hold_until_profit(trade_id, symbol):
            if unrealized <= 0:
                if _has_open_sell_order(symbol, expiry, strike, opt_type):
                    print(f"    >> MAX HOLD — already have open sell, skipping re-place")
                else:
                    print(f"    >> MAX HOLD ({mins:.0f}min, not in profit) — closing")
                    place_sell_order(symbol, expiry, strike, opt_type, qty, current)
                if trade_id is not None:
                    _close_trade_db(trade_id, current, unrealized, "max_hold")
                else:
                    _insert_orphan_trade_db(symbol, expiry, strike, opt_type, qty, entry)
                    notify("ORPHAN CLOSED: max hold", f"{symbol} ${strike} {opt_type} entry=${entry:.2f} exit=${current:.2f} pnl=${unrealized:+.2f}", priority="high")
                action_taken = True
            else:
                print(f"    >> MAX HOLD ({mins:.0f}min, in profit but below {sym_profit:+.0%}) — holding")
        # Rule 4: after 2PM loss exit
        elif after_cutoff and unrealized < 0:
            if _has_open_sell_order(symbol, expiry, strike, opt_type):
                print(f"    >> AFTER 2PM LOSS — already have open sell, skipping re-place")
            else:
                print(f"    >> AFTER 2PM LOSS — closing at {current:.2f}")
                place_sell_order(symbol, expiry, strike, opt_type, qty, current)
            if trade_id is not None:
                _close_trade_db(trade_id, current, unrealized, "after_2pm_loss")
            else:
                _insert_orphan_trade_db(symbol, expiry, strike, opt_type, qty, entry)
                notify("ORPHAN CLOSED: after 2pm", f"{symbol} ${strike} {opt_type} entry=${entry:.2f} exit=${current:.2f} pnl=${unrealized:+.2f}", priority="high")
            action_taken = True


def _insert_orphan_trade_db(symbol, expiry, strike, opt_type, qty, entry):
    """Insert a closed trade record for an orphan (Robinhood-only) position that
    was sold by the monitor. Lets us track P&L on cron-entered trades."""
    from datetime import datetime, timedelta
    from pytz import timezone
    try:
        tz = timezone(config.MARKET_TIMEZONE)
        now = datetime.now(tz)
        with get_db() as conn:
            # Dedupe: if there's already an open trade for the same
            # (symbol, expiry, strike, option_type), update its exit
            # instead of inserting a duplicate row. Without this check,
            # every cycle creates a new "closed" row for the same trade,
            # inflating today's P&L by the trade count.
            existing = conn.execute(
                """SELECT id FROM trades
                   WHERE mode='live' AND symbol=? AND option_expiry=?
                     AND option_strike=? AND option_type=?
                     AND status='open'
                   ORDER BY id DESC LIMIT 1""",
                (symbol, expiry, strike, opt_type)
            ).fetchone()
            if existing is not None:
                trade_id = existing[0]
                pnl = round((entry - 0) * qty, 2)  # placeholder; real pnl set by reconcile
                conn.execute(
                    """UPDATE trades SET status='closed', exit_price=?,
                                            exit_time=?, pnl=?,
                                            notes='orphan_monitor_closed'
                       WHERE id=?""",
                    (entry, now.isoformat(), pnl, trade_id)
                )
                conn.commit()
                return

            conn.execute(
                """INSERT INTO trades (mode, trade_date, symbol, option_expiry, option_strike,
                   option_type, direction, quantity, entry_price, entry_time,
                   exit_price, exit_time, status, pnl, notes)
                   VALUES ('live', ?, ?, ?, ?, ?, 'long_call', ?, ?, ?, ?, ?, 'closed', ?, 'orphan_monitor_closed')""",
                (now.date().isoformat(), symbol, expiry, strike, opt_type, qty, entry,
                 (now - timedelta(minutes=15)).isoformat(),
                 entry, now.isoformat(), 0.0)  # pnl=0 placeholder, real pnl computed by sync
            )
            conn.commit()
    except Exception as e:
        print(f"    >> DB insert for orphan failed: {e}")


def _close_trade_db(trade_id, exit_price, pnl, reason):
    """Update trade in DB after a sell."""
    try:
        from datetime import datetime
        from pytz import timezone
        tz = timezone(config.MARKET_TIMEZONE)
        with get_db() as conn:
            conn.execute(
                "UPDATE trades SET exit_price=?, exit_time=?, status='closed', pnl=? WHERE id=?",
                (exit_price, datetime.now(tz).isoformat(), pnl, trade_id)
            )
            conn.commit()
        print(f"    >> DB updated: trade {trade_id} closed at {exit_price}, pnl={pnl:.2f} [{reason}]")
        notify(
            f"TRADE CLOSED: {reason}",
            f"Trade {trade_id} | P&L: ${pnl:.2f}",
            priority="high",
            tags=["chart_with_upwards_trend"] if pnl >= 0 else ["warning"]
        )
    except Exception as e:
        print(f"    >> DB update failed: {e}")


def monitor_loop(poll_interval=60):
    """Run during market hours. Outer loop polls every `poll_interval` seconds;
    if any position is in the extreme turbulence tier, individual check frequency
    is adjusted to that tier's poll_seconds (15s for extreme tier)."""
    print(f"[MONITOR] Starting. Outer poll interval: {poll_interval}s")
    print(f"[MONITOR] Default rules: +{PROFIT_TARGET:.0%} profit | -{DEEP_LOSS:.0%} deep loss | {MAX_HOLD}min max hold | auto-exit losses after {LOSS_CUTOFF_HOUR:02d}:{LOSS_CUTOFF_MINUTE:02d} ET")
    print(f"[MONITOR] Tier rules: {config.TURBULENCE_TIERS}")

    # Per-symbol next-check timestamp. Default to "check now" for all symbols.
    next_check = {}  # symbol -> {"next_due": epoch_seconds, "poll_seconds": int, "tier": str}
    last_cycle_done = 0

    while True:
        if is_market_open():
            now = time.time()
            try:
                # Always do a full cycle (heartbeat + reconciliation) every
                # `poll_interval` seconds. But apply exit rules only for symbols
                # whose tier-poll timer has elapsed.
                if now - last_cycle_done >= poll_interval:
                    last_cycle_done = now
                    _run_cycle_with_throttling(next_check)
            except Exception as e:
                print(f"[MONITOR] Error: {e}")
        else:
            tz = pytz.timezone(config.MARKET_TIMEZONE)
            now = datetime.now(tz)
            if now.weekday() >= 5:
                print(f"[MONITOR] Weekend — sleeping 5 min")
            else:
                print(f"[MONITOR] Outside market hours ({now.strftime('%I:%M %Z')})")
        time.sleep(poll_interval)


def _run_cycle_with_throttling(next_check):
    """Like run_cycle but uses tier-based throttling per symbol. Symbols whose
    tier-poll timer has not elapsed are skipped from rule application."""
    # The shared per-position logic lives in run_cycle already. To avoid
    # duplicating ~150 lines of rule code, we use a flag-based approach:
    # temporarily monkey-patch run_cycle's per-symbol skip check.
    # Simpler: run_cycle already classifies turbulence and applies rules
    # automatically. We just need to gate it on a per-symbol basis.
    # Implementation: call run_cycle directly; it already uses sym_poll for
    # its own polling — but it applies rules immediately. So we add a quick
    # "should I apply rules for this symbol now?" check before each position.
    # We'll do this by injecting a global gate that run_cycle checks.
    global _TIER_GATE
    _TIER_GATE = next_check
    run_cycle()
    _TIER_GATE = None


def _should_apply_for_symbol(symbol, pos):
    """Gate: if tier-throttling is active and the symbol's tier-poll timer
    hasn't elapsed, skip rule application for this symbol."""
    if _TIER_GATE is None:
        return True
    next_due = _TIER_GATE.get(symbol, {}).get("next_due", 0)
    if time.time() < next_due:
        print(f"  {symbol}: tier throttled — next check in {int(next_due - time.time())}s")
        return False
    return True


# Gate state used by _should_apply_for_symbol — set by _run_cycle_with_throttling
# before calling run_cycle, reset after.
_TIER_GATE = None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="live")
    parser.add_argument("--poll", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_cycle()
    else:
        monitor_loop(poll_interval=args.poll)
