"""
Trader V2 daemon — the main orchestrator.

Single long-running process. Owns:
- Internal scheduler (no cron jobs)
- Position monitoring with tier-based polling
- Scanner (entry signals)
- Executor (order placement)
- Risk monitor (margin/concentration)
- Config hot-reload
- Graceful shutdown with cleanup

Run:  systemctl --user start trader-v2.service
Stop: systemctl --user stop trader-v2.service  (closes all positions, cancels orders)
"""
import sys
import os
import signal
import time
import logging
import threading
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/ralph/trader-v2")
# FIX 2026-08-10: Two-bot config loader — picks config.py or config_live.py
# based on TRADER_CONFIG_FILE env var (set by systemd service). The right
# config is loaded BEFORE any module-level imports so that executor/scanner
# pick up the correct MODE and ML settings.
import os as _os_for_config
_config_file = _os_for_config.environ.get("TRADER_CONFIG_FILE", "config.py")
if not _config_file.startswith("/"):
    _config_file = f"/home/ralph/trader-v2/{_config_file}"
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("config", _config_file)
config = _ilu.module_from_spec(_spec)
import sys as _sys_for_config
_sys_for_config.modules["config"] = config
_spec.loader.exec_module(config)

sys.path.insert(0, "/home/ralph/trader-v2/scripts")

import config
import policy_overlay as policy
try:
    import config_loader
except ImportError:
    # FIX 2026-08-13: legacy config_loader was expected to exist but the
    # module is gone. Provide a no-op stub so reload_if_changed is callable.
    class _ConfigLoaderStub:
        def reload_if_changed(self):
            pass
        def get(self, key, default=None):
            return default
    config_loader = _ConfigLoaderStub()  # FIX 2026-08-13: reads policy.json overlay for adaptive params
from database import get_connection, init_db
from robinhood_client import get_client
import executor
import scanner
import indicators

ET = ZoneInfo("US/Eastern")

# =============================================================================
# LOGGING
# =============================================================================
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("v2.daemon")


# =============================================================================
# STATE
# =============================================================================
shutdown_requested = False
last_scan_time = 0
last_risk_check_time = 0
last_account_refresh = 0
last_position_poll = {}  # position_id -> last_poll_time
last_trading_day = None

# Serialize all DB writes to avoid "database is locked" errors.
# Multiple poll_one_position + execute_sell calls can collide on the same
# positions row, causing SQLite lock contention. A single global lock
# ensures writes happen one at a time across the daemon's threads.
db_write_lock = threading.Lock()


def request_shutdown(signum, frame):
    global shutdown_requested
    log.info(f"Received signal {signum}, requesting shutdown...")
    shutdown_requested = True


signal.signal(signal.SIGTERM, request_shutdown)
signal.signal(signal.SIGINT, request_shutdown)


# =============================================================================
# HELPERS
# =============================================================================
def is_market_hours():
    """Check if market is open. Uses local time first (cheap), falls back to
    Robinhood API for accurate holiday detection.

    Tier 1 fix 2026-08-08: Use Robinhood get_market_status as authoritative
    source — handles holidays, early closes, and any schedule changes.
    """
    # Fast path: local time check
    now_et = datetime.now(ET)
    if now_et.weekday() not in config.TRADING_DAYS:
        return False
    now_time = now_et.time()
    open_time = dtime(*[int(x) for x in config.MARKET_OPEN_HHMM.split(":")])
    close_time = dtime(*[int(x) for x in config.MARKET_CLOSE_HHMM.split(":")])
    if not (open_time <= now_time < close_time):
        return False

    # Authoritative check: Robinhood API (handles holidays, early closes)
    if getattr(config, "USE_ROBINHOOD_MARKET_HOURS", True):
        try:
            client = get_client()
            status = client.get_market_status("XNYS")
            if status.get("is_open"):
                return True
            # API says closed — trust it over local time
            return False
        except Exception as e:
            # API failed — fall back to local time
            log.debug(f"Robinhood market status check failed: {e}")
            return True  # Local time said yes, trust it
    return True


def is_no_trade_window():
    if not config.ENABLE_NO_TRADE_WINDOW:
        return False
    now_et = datetime.now(ET).time()
    start = dtime(*[int(x) for x in config.NO_TRADE_START_HHMM.split(":")])
    end = dtime(*[int(x) for x in config.NO_TRADE_END_HHMM.split(":")])
    return start <= now_et < end


def reset_daily_state_if_new_day():
    """Reset circuit breaker and symbol cooldowns at start of new trading day."""
    global last_trading_day
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if last_trading_day == today:
        return
    last_trading_day = today

    # Authoritative check against the DB row, not just the in-memory flag:
    # a mid-day daemon restart used to zero realized_pnl here (fresh process =>
    # last_trading_day unset => unconditional reset), wiping the trading day.
    conn = get_connection()
    row = conn.execute("SELECT trading_day FROM circuit_breaker WHERE id=1").fetchone()
    if row and row["trading_day"] == today:
        # Same trading day already in DB (mid-day restart) — keep realized P&L.
        conn.close()
        return
    log.info(f"New trading day: {today}")
    # Reset circuit breaker row
    conn.execute("""
        INSERT OR REPLACE INTO circuit_breaker (id, trading_day, realized_pnl, tripped, last_updated)
        VALUES (1, ?, 0.0, 0, ?)
    """, (today, datetime.now(ET).isoformat()))
    conn.commit()
    conn.close()


def eod_flatten():
    """If within the EOD flatten window, close all positions."""
    now_et = datetime.now(ET).time()
    flatten_time = dtime(*[int(x) for x in config.EOD_FLATTEN_HHMM.split(":")])
    if now_et >= flatten_time:
        conn = get_connection()
        positions = conn.execute("SELECT * FROM positions").fetchall()
        conn.close()
        if positions:
            log.info(f"EOD flatten triggered, closing {len(positions)} positions")
            executor.close_all_positions(reason="eod_flatten")

        # 2026-09-21: also cancel pending paper buys at EOD — unfilled patient
        # bids must not survive overnight.
        try:
            executor.cancel_pending_paper_buys(reason="eod")
        except Exception as _e:
            log.debug(f"pending-buys EOD cancel skipped: {_e}")


# =============================================================================
# SCAN CYCLE (entry decisions)
# =============================================================================
def run_scan_cycle():
    """Run a scan cycle. Skip if not market hours or in no-trade window."""
    global last_scan_time

    now = time.time()
    if now - last_scan_time < config.SCAN_INTERVAL:
        return
    last_scan_time = now

    if not is_market_hours():
        log.info("Scan skipped: not market hours")
        return

    # FIX 2026-08-13: drift check — auto-freeze policy if regime shifted bad
    try:
        from drift_detector import check_paper_drift
        drift_info = check_paper_drift()
        if drift_info.get('drift') and drift_info.get('direction') == 'old_better':
            _p = policy.load_policy()
            if not _p.get('frozen'):
                log.warning(f"DRIFT DETECTED (old_better). Auto-freezing policy. {drift_info}")
                _p['frozen'] = True
                _p['adapt_reason'] = 'drift_old_better_freeze_realtime'
                policy.save_policy(_p, source='drift_freeze_realtime')
    except Exception as _e:
        log.debug(f"drift check skipped: {_e}")

    if is_no_trade_window():
        log.info("Scan skipped: no-trade window (9:30-9:45)")
        return

    log.info("=== Scan cycle ===")
    try:
        candidates = scanner.scan()
        log.info(f"Scan found {len(candidates)} candidates")

        # FIX 2026-08-15: market-wide cooldown after losing streak.
        # Per-symbol cooldowns didn't help during Aug 6 / Aug 12 events
        # where 5+ symbols all hit loss_cap within minutes (regime shift).
        # Pause all entries when: (a) 3+ losses in last 30 min OR
        # (b) realized P&L today <= -$200
        try:
            from database import get_connection
            cutoff = (datetime.now(ET) - timedelta(minutes=30)).isoformat()
            with get_connection() as conn:
                recent_losses = conn.execute("""
                    SELECT COUNT(*) FROM trades
                    WHERE mode = 'paper'
                      AND timestamp_close >= ?
                      AND net_pnl < 0
                """, (cutoff,)).fetchone()[0]
                today = datetime.now(ET).strftime("%Y-%m-%d")
                today_pnl = conn.execute("""
                    SELECT COALESCE(SUM(net_pnl), 0) FROM trades
                    WHERE mode = 'paper' AND DATE(timestamp_close) = ?
                """, (today,)).fetchone()[0]
            if recent_losses >= 3:
                log.warning(f"Global cooldown: {recent_losses} losses in last 30 min — skipping scan")
                return
            _daily_loss_pause = abs(float(getattr(config, "DAILY_LOSS_PAUSE", 200)))
            if _daily_loss_pause > 0 and today_pnl <= -_daily_loss_pause:
                log.warning(f"Daily loss ${today_pnl:.2f} <= -${_daily_loss_pause:.0f} — pausing entries")
                return
            if _daily_loss_pause <= 0:
                log.debug(f"Daily-loss pause disabled (DAILY_LOSS_PAUSE <= 0); today_pnl=${today_pnl:.2f}")
        except Exception as e:
            log.debug(f"global cooldown check failed: {e}")

        # Execute buys for top candidates
        for sig in candidates:
            if shutdown_requested:
                break
            try:
                trade_id = executor.execute_buy(sig)
                if trade_id:
                    # FIX 2026-08-07b: Record the buy time so orphan-sync
                    # won't race with the just-placed order.
                    _last_buy_time[sig["symbol"].upper()] = time.time()
                    _save_buy_times()
                    log.info(f"Opened trade {trade_id}: {sig['symbol']} {sig['direction']}")
            except Exception as e:
                log.error(f"execute_buy failed for {sig['symbol']}: {e}")
    except Exception as e:
        log.error(f"Scan cycle failed: {e}", exc_info=True)


# =============================================================================
# RISK MONITOR
# =============================================================================
def run_risk_check():
    """Margin + concentration check."""
    global last_risk_check_time

    now = time.time()
    if now - last_risk_check_time < config.RISK_CHECK_INTERVAL:
        return
    last_risk_check_time = now

    if not is_market_hours():
        return

    try:
        client = get_client()
        account = client.get_account_state()
        if not account:
            return

        equity = account.get("equity", 0)
        excess = account.get("excess_margin", 0)

        # Margin alerts
        if equity > 0 and excess < 0:
            log.error(f"MARGIN ALERT: excess_margin=${excess:.2f}")
        elif equity > 0 and excess < equity * 0.10:
            log.warning(f"Low margin: excess=${excess:.2f}, equity=${equity:.2f}")

        # Concentration
        conn = get_connection()
        positions = conn.execute("SELECT * FROM positions").fetchall()
        conn.close()
        for pos in positions:
            client = get_client()
            market = client.get_option_market_data(
                pos["symbol"], pos["option_expiry"], pos["option_strike"], pos["option_type"]
            )
            if not market or market["bid"] <= 0:
                continue
            position_value = market["bid"] * pos["quantity"] * 100
            concentration = (position_value / equity * 100) if equity > 0 else 0
            if concentration > 20:
                log.warning(f"Concentration alert: {pos['symbol']} = {concentration:.1f}% of equity")
    except Exception as e:
        log.error(f"Risk check failed: {e}", exc_info=True)


# =============================================================================
# POSITION MONITOR (exit decisions, tier-based polling)
# =============================================================================

# FIX 2026-08-07: Orphan-sync — track consecutive sell failures per position
# so the daemon gives up after N rejections instead of spamming Robinhood.
_sell_failure_counts = {}  # position_id -> failure count
SELL_FAIL_GIVEUP_THRESHOLD = 3  # Give up after 3 consecutive rejections

# FIX 2026-08-07: Track the last buy time per symbol so orphan-sync doesn't
# race with a just-placed order. Robinhood takes 1-3 seconds to register a new
# position, so we skip orphan-sync for symbols bought in the last 30 seconds.
# Persisted to disk so it survives daemon restarts.
import json as _json
_BUY_TIME_FILE = "/tmp/trader_last_buy_time.json"

def _load_buy_times():
    try:
        with open(_BUY_TIME_FILE) as f:
            data = _json.load(f)
            # Drop entries older than 5 minutes (stale)
            cutoff = time.time() - 300
            return {k: v for k, v in data.items() if v > cutoff}
    except Exception:
        return {}

def _save_buy_times():
    try:
        with open(_BUY_TIME_FILE, "w") as f:
            _json.dump(_last_buy_time, f)
    except Exception:
        pass

_last_buy_time = _load_buy_times()  # symbol -> unix timestamp of last buy
ORPHAN_SYNC_GRACE_SECONDS = 30  # Don't orphan-check symbols bought recently


def sync_positions_with_robinhood():
    """
    FIX 2026-08-07: Reconcile local DB positions with Robinhood.

    If Robinhood reports no open positions but the DB has rows, the DB is
    stale (position expired, was manually closed, or Robinhood auto-cleaned
    it up). Mark those positions as closed with exit_reason='orphan_sync'
    so the daemon stops trying to sell them.

    This prevents the spam-loop we hit today: the daemon spent 3 minutes
    sending sell orders for a position Robinhood no longer knew about.

    FIX 2026-08-07b: Skip symbols we just bought. Robinhood takes 1-3s to
    register a new position, so checking immediately after a buy causes
    false-positive orphan detection.
    """
    now_ts = time.time()
    try:
        client = get_client()
        rh_positions = client.get_open_option_positions() or []

        # Build set of (symbol, strike, type, expiry) tuples Robinhood has open
        rh_open = set()
        for p in rh_positions:
            sym = (p.get("symbol") or "").upper()
            strike = p.get("strike")
            opt_type = (p.get("option_type") or "").lower()
            expiry = p.get("expiry")
            qty = float(p.get("quantity") or 0)
            if sym and strike and opt_type and expiry and qty != 0:
                rh_open.add((sym, float(strike), opt_type[0], expiry))

        conn = get_connection()
        local_positions = conn.execute("SELECT * FROM positions").fetchall()
        orphans_found = 0
        for pos in local_positions:
            # FIX 2026-08-10: Skip paper/system positions — they do not exist
            # on Robinhood so comparing to RH will always be a false-positive.
            # Includes both 'system' (single-bot legacy) and 'system_paper' /
            # 'system_live' (two-bot split). Only 'rh_sync' positions are
            # checked against Robinhood.
            pos_source = pos["source"] if pos["source"] else ""
            if pos_source in ("system", "system_paper", "system_live"):
                continue

            # FIX 2026-08-07b: Skip if we just bought this symbol — Robinhood
            # takes 1-3s to register the new position.
            sym = pos["symbol"].upper()
            if sym in _last_buy_time:
                age = now_ts - _last_buy_time[sym]
                if age < ORPHAN_SYNC_GRACE_SECONDS:
                    log.debug(f"[ORPHAN-SYNC] Skipping {sym} "
                              f"(bought {age:.1f}s ago, grace {ORPHAN_SYNC_GRACE_SECONDS}s)")
                    continue

            key = (
                pos["symbol"].upper(),
                float(pos["option_strike"]),
                pos["option_type"][0].lower(),
                pos["option_expiry"],
            )
            if key not in rh_open:
                # Robinhood doesn't have this position — it's an orphan
                log.warning(
                    f"[ORPHAN-SYNC] Position {pos['id']} {pos['symbol']} ${pos['option_strike']} "
                    f"{pos['option_type']} not found on Robinhood — marking closed"
                )
                # Use last polled price if available, else entry price
                # sqlite3.Row doesn't have .get() — use bracket notation
                last_poll = pos["last_poll_price"] if pos["last_poll_price"] else pos["entry_price"]
                exit_price = last_poll if last_poll else pos["entry_price"]
                pnl = (exit_price - pos["entry_price"]) * pos["quantity"] * 100

                # Move to trades table
                conn.execute("""
                    INSERT INTO trades (
                        symbol, option_type, option_strike, option_expiry,
                        quantity, entry_price, exit_price, pnl, exit_reason,
                        mode, timestamp_open, timestamp_close
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pos["symbol"], pos["option_type"], pos["option_strike"],
                    pos["option_expiry"], pos["quantity"], pos["entry_price"],
                    exit_price, pnl, "orphan_sync",
                    config.MODE,
                    pos["entry_time"], datetime.now(ET).isoformat(),
                ))
                # Remove from positions
                conn.execute("DELETE FROM positions WHERE id = ?", (pos["id"],))
                # Clear failure counter for this position
                _sell_failure_counts.pop(pos["id"], None)
                orphans_found += 1

        conn.commit()
        conn.close()
        if orphans_found:
            log.info(f"[ORPHAN-SYNC] Cleaned up {orphans_found} orphan position(s)")
        return orphans_found
    except Exception as e:
        log.error(f"[ORPHAN-SYNC] Failed: {e}")
        return 0


def run_position_polls():
    """Poll each open position according to its tier."""
    # FIX 2026-08-07: Run orphan-sync before any polls so we don't waste
    # cycles trying to sell positions Robinhood doesn't recognize.
    sync_positions_with_robinhood()

    conn = get_connection()
    # FIX 2026-08-10: Two-bot split — each bot only manages its own positions.
    # Paper bot only polls system_paper rows; live bot only polls system_live.
    # Prevents cross-bot interference.
    bot_mode = getattr(config, "MODE", "paper").lower()
    my_source = f"system_{bot_mode}"
    positions = conn.execute(
        "SELECT * FROM positions WHERE source = ? OR source = 'rh_sync'", (my_source,)
    ).fetchall()
    conn.close()

    now = time.time()
    for pos in positions:
        tier = pos["tier"]
        interval = config.POLL_TIERS.get(tier, config.POLL_TIERS["standard"])
        last = last_position_poll.get(pos["id"], 0)
        if now - last < interval:
            continue
        last_position_poll[pos["id"]] = now
        # Serialize DB writes across all positions to prevent lock contention.
        # Without this, two positions being polled simultaneously can collide.
        try:
            with db_write_lock:
                poll_one_position(dict(pos))
        except Exception as e:
            log.error(f"Poll failed for position {pos['id']}: {e}")


def poll_one_position(pos):
    """Apply exit rules to one position."""
    client = get_client()
    market = client.get_option_market_data(
        pos["symbol"], pos["option_expiry"], pos["option_strike"], pos["option_type"]
    )
    if not market or market["bid"] <= 0:
        return

    # FIX 2026-08-05: Use bid price for P&L check (realistic exit price)
    # Previously used mid (bid+ask)/2 which ignores spread. When entry was
    # at mid but exit sells at bid, P&L was overstated. Now uses bid which
    # is what we'd actually get if we sold right now.
    current_price = market["bid"]
    if current_price <= 0:
        return

    entry = pos["entry_price"]
    pnl_pct = (current_price - entry) / entry
    pnl_dollar = (current_price - entry) * pos["quantity"] * 100
    tier = pos["tier"]

    # Update last poll price in DB
    conn = get_connection()
    conn.execute("""
        UPDATE positions
        SET last_poll_at=?, last_poll_price=?
        WHERE id=?
    """, (datetime.now(ET).isoformat(), current_price, pos["id"]))
    conn.commit()
    conn.close()

    # 2026-09-17 (Ralph): trade lifecycle audit — stamp first time in profit,
    # first time in loss, and running peak/trough on the trade row.
    try:
        _now_et = datetime.now(ET).isoformat()
        conn = get_connection()
        _open_row = conn.execute("""
            SELECT id, time_first_profit, price_first_profit, time_first_loss, price_first_loss,
                   peak_price, peak_time, trough_price, trough_time
            FROM trades
            WHERE symbol=? AND option_strike=? AND option_expiry=? AND option_type=?
              AND exit_price IS NULL
            ORDER BY timestamp_open DESC LIMIT 1
        """, (pos["symbol"], pos["option_strike"], pos["option_expiry"], pos["option_type"])).fetchone()
        if _open_row:
            _tid = _open_row["id"]
            _sets, _args = [], []
            if current_price > entry and _open_row["time_first_profit"] is None:
                _sets += ["time_first_profit=?", "price_first_profit=?"]
                _args += [_now_et, current_price]
            if current_price < entry and _open_row["time_first_loss"] is None:
                _sets += ["time_first_loss=?", "price_first_loss=?"]
                _args += [_now_et, current_price]
            if _open_row["peak_price"] is None or current_price > _open_row["peak_price"]:
                _sets += ["peak_price=?", "peak_time=?"]
                _args += [current_price, _now_et]
            if _open_row["trough_price"] is None or current_price < _open_row["trough_price"]:
                _sets += ["trough_price=?", "trough_time=?"]
                _args += [current_price, _now_et]
            if _sets:
                _args.append(_tid)
                conn.execute(f"UPDATE trades SET {', '.join(_sets)} WHERE id=?", _args)
                conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[LIFECYCLE] audit stamp failed for {pos['symbol']}: {e}")

    # FIX 2026-08-06: HOLD flag — when set, override ALL auto-sell rules.
    # User explicitly marked this position as held (e.g. "I'm holding RKLB").
    # Only manual close via dashboard button or `unhold` can release it.
    on_hold = pos.get("on_hold", 0) if hasattr(pos, 'get') else 0
    if on_hold:
        log.info(f"[{pos['symbol']}] ON HOLD — skipping auto-exit checks (current P&L: ${pnl_dollar:+.2f})")
        return  # do not evaluate any exit rule below

    # FIX 2026-08-05: Custom target exit price (set by user for specific positions)
    # If target_exit_price is set and current bid >= target, sell at target.
    target_exit = pos.get("target_exit_price") if hasattr(pos, 'get') else None
    if target_exit and current_price >= target_exit:
        log.info(f"[{pos['symbol']}] target hit: bid=${current_price:.2f} >= target=${target_exit:.2f}")
        executor.execute_sell(pos["id"], reason="target_exit")
        return

    # --- V2 EXIT RULES (FIX 2026-08-10 — dollar-based, hold losers) ---
    # Priority order (most important first):
    # 1. MAX_PROFIT_DOLLAR (+$30): sell — took max win
    # 2. TAKE_PROFIT_DOLLAR (+$10): sell — covers recovery-from-loss OR
    #    drop-back-from-peak-$20 (user logic: $20→drop→$10 = take the $10)
    # 3. EOD cutoff (3:55 PM ET): sell — 0DTE close before pin risk
    # 4. LOSS_CAP_PCT_NEW (-50% of entry): sell — cap the loss
    # 5. RSI exit: existing RSI-based exit still runs

    # 2026-09-16: per-symbol overrides (symbol_settings.py) — page globals remain the fallback
    import symbol_settings as _ss
    _sym = pos.get("symbol", "")

    # Rule 1: Max profit
    _max_profit_val = _ss.get(_sym, 'max_profit', policy.get_for_symbol('MAX_PROFIT_DOLLAR', config.MAX_PROFIT_DOLLAR, _sym))
    if pnl_dollar >= _max_profit_val:
        log.info(f"[{pos['symbol']}] MAX PROFIT: +${pnl_dollar:.2f} >= ${_max_profit_val:.0f} — selling")
        executor.execute_sell(pos["id"], reason="max_profit")
        return

    # Rule 2: Take profit at +$10
    _tp_val = _ss.get(_sym, 'take_profit', policy.get_for_symbol('TAKE_PROFIT_DOLLAR', config.TAKE_PROFIT_DOLLAR, _sym))
    if pnl_dollar >= _tp_val:
        log.info(f"[{pos['symbol']}] TAKE PROFIT: +${pnl_dollar:.2f} >= ${_tp_val:.0f} — selling")
        executor.execute_sell(pos["id"], reason="take_profit")
        return

    # Rule 3: EOD cutoff for 0DTE options
    from datetime import datetime as _dt
    expiry_date = pos["option_expiry"]  # YYYY-MM-DD string
    try:
        # 0DTE = expiry date is today (ET)
        today_et = datetime.now(ET).strftime("%Y-%m-%d")
        if expiry_date == today_et:
            now_hhmm = datetime.now(ET).strftime("%H:%M")
            if now_hhmm >= config.EOD_CUTOFF_HHMM:
                log.info(f"[{pos['symbol']}] 0DTE EOD CUTOFF: {now_hhmm} >= {config.EOD_CUTOFF_HHMM} — selling")
                executor.execute_sell(pos["id"], reason="eod_cutoff")
                return
    except Exception:
        pass  # fall through if date parsing fails

    # Rule 4: Dollar-adaptive loss cap (FIX 2026-08-12; 2026-09-16: settings-page governance)
    # GOVERNING KEYS are the settings-page ones: LOSS_CAP_DOLLAR (page "$ cap") and
    # LOSS_CAP_PCT (page "% of premium cap"). V2 names remain as legacy fallbacks.
    # Effective cap = SMALLER of the $ cap and the %-of-premium cap (page globals; per-symbol overrides)
    if pos.get("entry_price") and pos["entry_price"] > 0:
        # per-symbol override -> policy overlay (tuned symbols only) -> page global
        eff_dollar = _ss.get(_sym, 'max_loss_dollar',
                             policy.get_for_symbol('LOSS_CAP_DOLLAR_V2',
                                                   getattr(config, 'LOSS_CAP_DOLLAR',
                                                           getattr(config, 'LOSS_CAP_DOLLAR_V2', 30.0)), _sym))
        eff_pct = _ss.get(_sym, 'max_loss_pct',
                          getattr(config, 'LOSS_CAP_PCT', getattr(config, 'LOSS_CAP_PCT_V2', 0.25)))
        pct_cap = pos["entry_price"] * 100 * pos.get("quantity", 1) * eff_pct
        dollar_cap = min(eff_dollar, pct_cap)
    else:
        dollar_cap = _ss.get(_sym, 'max_loss_dollar',
                             policy.get_for_symbol('LOSS_CAP_DOLLAR_V2', config.LOSS_CAP_DOLLAR_V2, _sym))
    if pnl_dollar <= -dollar_cap:
        log.info(f"[{pos['symbol']}] LOSS CAP: ${pnl_dollar:.2f} <= -${dollar_cap:.2f} — selling")
        executor.execute_sell(pos["id"], reason="loss_cap_v2")
        return

    # Rule 4b: per-symbol hold time (optional; global has no hold limit)
    _hold_min = _ss.get(_sym, 'hold_time_min', None)
    if _hold_min:
        from datetime import datetime as _dtmod
        try:
            _opened = _dtmod.fromisoformat(pos["entry_time"])
            _held = (_dtmod.now(_opened.tzinfo) - _opened).total_seconds() / 60.0
            if _held >= float(_hold_min):
                log.info(f"[{pos['symbol']}] HOLD TIME: {_held:.0f}m >= {_hold_min}m — selling")
                executor.execute_sell(pos["id"], reason="hold_time")
                return
        except Exception:
            pass  # bad timestamp -> skip hold check

    # Note: MAX_HOLD_MINUTES removed — V2 lets trades ride to expiration
    # unless exit rules 1-4 fire. If a trade is under -50% and we're at
    # expiration, it'll either rule 3 (EOD) fire or expire worthless.

    # 4. FIX 2026-08-06: RSI-based exit (uses Robinhood 5-min candles)
    # Exit on overbought/oversold extremes + divergence detection.
    # Faster than waiting for profit target; can capture gains before reversal.
    if getattr(config, "USE_RSI_EXIT", True):
        try:
            from rsi_exit import check_rsi_exit
            rsi_result = check_rsi_exit({
                "symbol": pos["symbol"],
                "option_type": pos.get("option_type", "call"),
                "entry_price": entry,
                "current_price": current_price,
                "quantity": pos["quantity"],
            })
            if rsi_result and rsi_result.get("should_exit"):
                log.info(f"[{pos['symbol']}] RSI exit: {rsi_result['detail']}")
                executor.execute_sell(pos["id"], reason=rsi_result["reason"])
                return
            elif rsi_result:
                # Log RSI info for analysis (debug level)
                log.debug(f"[{pos['symbol']}] RSI={rsi_result['rsi']:.1f} "
                          f"div={rsi_result.get('divergence')}")
        except Exception as e:
            log.debug(f"[{pos['symbol']}] RSI check failed: {e}")


# =============================================================================
# ACCOUNT REFRESH
# =============================================================================
def run_account_refresh():
    global last_account_refresh
    now = time.time()
    if now - last_account_refresh < config.ACCOUNT_REFRESH:
        return
    last_account_refresh = now

    try:
        client = get_client()
        account = client.get_account_state()
        if account:
            log.debug(f"Account refresh: equity=${account.get('equity', 0):.2f}")
    except Exception as e:
        log.error(f"Account refresh failed: {e}")


# =============================================================================
# SHUTDOWN
# =============================================================================
def graceful_shutdown():
    log.info("Starting graceful shutdown...")
    if config.CLEANUP_ON_STOP:
        log.info("Cancelling pending orders...")
        executor.cancel_all_pending_orders()
        log.info("Closing all positions...")
        executor.close_all_positions(reason="shutdown_cleanup")

        # FIX 2026-08-05: Verify no positions remain open after cleanup.
        # execute_sell now waits for fill confirmation before returning True.
        # If any position couldn't be sold, it stays in DB and would be
        # reopened on next daemon start. Block shutdown until verified.
        conn = get_connection()
        remaining = conn.execute("SELECT * FROM positions").fetchall()
        conn.close()
        if remaining:
            log.error(f"[SHUTDOWN] {len(remaining)} positions still open after cleanup:")
            for pos in remaining:
                sym = pos['symbol']
                strike = pos['option_strike']
                otype = pos['option_type']
                pnl_pct = "?"  # We don't know current PnL here
                log.error(f"[SHUTDOWN]   {sym} ${strike} {otype} qty={pos['quantity']}")
            log.error("[SHUTDOWN] Verify these on Robinhood manually before restart")
            # Block for up to 30s waiting for any remaining positions to fill
            import time as _time
            for _ in range(6):
                _time.sleep(5)
                conn = get_connection()
                remaining = conn.execute("SELECT * FROM positions").fetchall()
                conn.close()
                if not remaining:
                    log.info("[SHUTDOWN] All positions verified closed")
                    break
            else:
                log.error(f"[SHUTDOWN] {len(remaining)} positions still open after wait — manual cleanup needed")
        else:
            log.info("[SHUTDOWN] All positions verified closed")
    log.info("Shutdown complete")


# =============================================================================
# MAIN LOOP
# =============================================================================
def main():
    log.info("=" * 60)
    log.info("Trader V2 daemon starting")
    log.info("=" * 60)

    # Loud startup banner so it's obvious what mode we're in.
    mode = config.MODE.lower().strip()
    if mode == "live":
        log.warning("=" * 60)
        log.warning("⚠️  STARTING IN LIVE MODE — REAL MONEY ORDERS ⚠️")
        log.warning(f"⚠️  config.MODE = 'live' (TRADER_CONFIG_FILE={os.environ.get('TRADER_CONFIG_FILE', 'config.py')}) ⚠️")
        log.warning("⚠️  To go back to paper: systemctl --user stop trader-v2-live.service ⚠️")
        log.warning("=" * 60)
    elif mode == "paper":
        log.info("[MODE] paper — simulated orders, no real money")
    else:
        log.error(f"[MODE] Unknown MODE={mode!r} — refusing to start. "
                  f"Set config.MODE = 'paper' or 'live'.")
        sys.exit(1)

    # Initialize DB
    init_db()

    # Login (with 2FA if needed)
    client = get_client()
    result = client.login()
    log.info(f"Login result: {result}")

    if result["status"] == "mfa_required":
        log.warning("=" * 60)
        log.warning("MFA REQUIRED — please approve on your phone")
        log.warning("Once approved, the dashboard will prompt for the MFA code")
        log.warning("Or restart the daemon with: systemctl --user restart trader-v2.service")
        log.warning("=" * 60)
        # Don't exit — daemon stays running so dashboard can collect the code
        # The dashboard will call client.login(mfa_code=...) when user enters it
        while not shutdown_requested:
            time.sleep(1)
        graceful_shutdown()
        return

    if result["status"] == "error":
        log.error(f"Login failed: {result['detail']}")
        # Still keep daemon alive so user can retry via dashboard
        while not shutdown_requested:
            time.sleep(1)
        return

    log.info(f"Logged in successfully: {result['detail']}")

    # FIX 2026-08-05: Place target exit orders for any positions with target_exit_price set.
    # This handles cases like user wanting to sell RKLB @ $2.41 — the daemon places
    # the limit order at startup so the sell is queued (GTC) until filled.
    # FIX 2026-09-14: ONLY in live mode. In paper mode these positions are
    # simulated (paper DB) — the poll loop already sells them as paper fills,
    # and real GTC orders on Robinhood are invalid ("not enough contracts").
    if getattr(config, "MODE", "paper").lower().strip() == "live":
        try:
            conn = get_connection()
            positions_with_target = conn.execute(
                "SELECT * FROM positions WHERE target_exit_price IS NOT NULL AND on_hold = 0"
            ).fetchall()
            conn.close()
            for pos in positions_with_target:
                target = pos["target_exit_price"]
                log.info(f"[STARTUP] Placing GTC sell for {pos['symbol']} ${pos['option_strike']} "
                         f"x{pos['quantity']} @ ${target}")
                order = client.order_sell_option_limit(
                    symbol=pos["symbol"],
                    expiry=pos["option_expiry"],
                    strike=pos["option_strike"],
                    option_type=pos["option_type"],
                    quantity=pos["quantity"],
                    limit_price=target
                )
                if order and order.get("id"):
                    log.info(f"[STARTUP] Sell order placed: {order.get('id')} @ ${target}")
                else:
                    log.error(f"[STARTUP] Sell order FAILED: {order}")
        except Exception as e:
            log.error(f"[STARTUP] Error placing target exit orders: {e}", exc_info=True)
    else:
        log.info("[STARTUP] paper mode: skipping GTC sell placement (poll loop handles "
                 "target exits as paper fills)")

    # Main loop — 1 second tick
    last_heartbeat = 0
    while not shutdown_requested:
        try:
            # 1. Hot-reload config (every tick is fine, cheap)
            config_loader.reload_if_changed()

            # 2. Reset daily state if new day
            reset_daily_state_if_new_day()

            # 3. EOD flatten if within window
            eod_flatten()

            # 4. Account refresh
            run_account_refresh()

            # 5. Risk monitor
            run_risk_check()

            # 6. Scan cycle (every SCAN_INTERVAL seconds)
            run_scan_cycle()

            # 7. Position monitor (every 1s, but each position has its own tier interval)
            run_position_polls()

            # 7b. Pending paper buys (2026-09-21): fill queued patient bids
            # when the ask drops to the limit (realistic paper fills).
            try:
                executor.check_pending_paper_buys()
            except Exception as _e:
                log.debug(f"pending paper buys check skipped: {_e}")

            # 8. Heartbeat every 30s so we know the loop is alive
            if time.time() - last_heartbeat > 30:
                last_heartbeat = time.time()
                log.debug(f"Heartbeat: market_hours={is_market_hours()}, "
                          f"no_trade_window={is_no_trade_window()}")

        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)

        time.sleep(1)

    graceful_shutdown()


if __name__ == "__main__":
    # FIX 2026-08-10: Two-bot config already loaded at module import time
    # via TRADER_CONFIG_FILE env var. --mode flag is just for the runtime
    # check (in case you want paper config but want to override MODE).
    import argparse
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["paper", "live"], default=None,
        help="Override MODE without changing config files. "
             "Note: TRADER_CONFIG_FILE (set by systemd service) picks the "
             "config file; --mode only flips the MODE flag."
    )
    args = parser.parse_args()

    log.info(f"[BOOT] Using TRADER_CONFIG_FILE={os.environ.get('TRADER_CONFIG_FILE', 'config.py')}, "
             f"current MODE={config.MODE}")

    if args.mode is not None:
        config.MODE = args.mode
        log.info(f"[MODE] Override from CLI: MODE={args.mode}")

    main()
