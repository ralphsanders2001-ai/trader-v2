"""
Database schema and connection helpers for Trader V2.
"""
import sqlite3
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, "/home/ralph/trader-v2")
import config

ET = ZoneInfo("America/New_York")


def get_connection():
    """Get a SQLite connection with row factory. Retries on lock.

    Uses DELETE journal mode instead of WAL. WAL caused intermittent
    'database is locked' errors because writes from multiple connections
    serialize through the WAL file and reads can race with the daemon's
    polling writes. DELETE mode is simpler for single-process daemons.
    """
    import time as _t
    for attempt in range(5):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            conn = sqlite3.connect(config.DB_PATH, timeout=30)
            conn.row_factory = sqlite3.Row
            # DELETE journal — simpler than WAL, no contention issues
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                _t.sleep(0.05 * (attempt + 1))
                continue
            raise


SCHEMA = """
-- ============================================================================
-- TRADES (the main trade ledger)
-- ============================================================================
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_open TEXT NOT NULL,
    timestamp_close TEXT,
    mode TEXT NOT NULL DEFAULT 'live',  -- 'live' or 'paper' (for future)
    symbol TEXT NOT NULL,
    option_type TEXT NOT NULL,         -- 'call' or 'put'
    option_strike REAL NOT NULL,
    option_expiry TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    entry_price REAL NOT NULL,
    exit_price REAL,
    pnl REAL,
    exit_reason TEXT,                  -- 'profit_target', 'loss_cap', 'max_hold',
                                       -- 'eod_flatten', 'manual', 'circuit_breaker',
                                       -- 'orphan_recovery', 'shutdown_cleanup'
    entry_signal_score INTEGER,        -- score from signal generator
    entry_signal_components TEXT,      -- JSON of indicator breakdown
    entry_candles_used TEXT,           -- JSON of 5m candles at entry (for replay)
    exit_signal_score INTEGER,
    robinhood_order_id TEXT,           -- order ID from Robinhood (NULL = phantom)
    exit_order_id TEXT,                -- exit order ID from Robinhood (NULL = phantom)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_mode_status ON trades(mode, exit_reason);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_open ON trades(symbol, timestamp_close);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp_close ON trades(timestamp_close);

-- ============================================================================
-- POSITIONS (current open positions — mirrors Robinhood)
-- ============================================================================
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    option_type TEXT NOT NULL,
    option_strike REAL NOT NULL,
    option_expiry TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    robinhood_position_id TEXT,
    robinhood_instrument_id TEXT,
    tier TEXT NOT NULL DEFAULT 'standard',  -- 'high_velocity', 'standard', 'low_volatility'
    last_poll_at TEXT,
    last_poll_price REAL,
    source TEXT NOT NULL DEFAULT 'system',   -- 'system' or 'orphan_recovery'
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, option_strike, option_expiry, option_type)
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_tier ON positions(tier);

-- ============================================================================
-- SYMBOL DAILY STATE (cooldowns, win streaks, blocks)
-- ============================================================================
CREATE TABLE IF NOT EXISTS symbol_daily_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    trading_day TEXT NOT NULL,              -- '2026-07-30'
    losses_today INTEGER NOT NULL DEFAULT 0,
    wins_today INTEGER NOT NULL DEFAULT 0,
    consecutive_wins INTEGER NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,                   -- ISO datetime
    blocked_for_day INTEGER NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    last_trade_time TEXT,
    UNIQUE(symbol, trading_day)
);

CREATE INDEX IF NOT EXISTS idx_symbol_state_day ON symbol_daily_state(trading_day);
CREATE INDEX IF NOT EXISTS idx_symbol_state_cooldown ON symbol_daily_state(cooldown_until);

-- ============================================================================
-- CIRCUIT BREAKER STATE (global)
-- ============================================================================
CREATE TABLE IF NOT EXISTS circuit_breaker (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- only one row
    trading_day TEXT NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    tripped INTEGER NOT NULL DEFAULT 0,
    tripped_at TEXT,
    reason TEXT,
    last_updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- SIGNALS (audit log of every signal generated, even rejected ones)
-- ============================================================================
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candles_used INTEGER NOT NULL,
    score INTEGER NOT NULL,
    components TEXT,                        -- JSON of indicator breakdown
    direction TEXT NOT NULL,                -- 'long_call', 'long_put', 'none'
    status TEXT NOT NULL DEFAULT 'rejected', -- 'rejected' or 'taken'
    rejection_reason TEXT,
    trade_id INTEGER,
    mode TEXT DEFAULT 'live',               -- 'live' or 'paper' — paper/live DB separation (2026-08-10)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

-- ============================================================================
-- CONFIG CHANGES (history of every edit)
-- ============================================================================
CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL DEFAULT 'dashboard'  -- 'dashboard' or 'manual'
);

CREATE INDEX IF NOT EXISTS idx_config_changes_timestamp ON config_changes(timestamp);

-- ============================================================================
-- DAILY SUMMARY (end-of-day rollup)
-- ============================================================================
CREATE TABLE IF NOT EXISTS daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day TEXT NOT NULL UNIQUE,
    total_trades INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    losing_trades INTEGER NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0.0,
    best_trade_pnl REAL,
    worst_trade_pnl REAL,
    symbols_traded TEXT,                -- comma-separated
    circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- TODO LIST (in-app to-dos)
-- ============================================================================
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_number TEXT NOT NULL UNIQUE,        -- e.g. '2201-001'
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',        -- 'open', 'in_progress', 'closed'
    priority TEXT NOT NULL DEFAULT 'normal',     -- 'low', 'normal', 'high'
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
"""


def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    migrate_db()
    print(f"[DB] Initialized at {config.DB_PATH}")


def migrate_db():
    """Apply schema migrations for databases that already exist.

    SQLite ALTER TABLE ADD COLUMN is idempotent-friendly via try/except.
    """
    conn = get_connection()
    cur = conn.cursor()

    migrations = [
        # 2026-07-31: add order ID columns to verify real vs phantom trades
        ("ALTER TABLE trades ADD COLUMN robinhood_order_id TEXT", "trades"),
        ("ALTER TABLE trades ADD COLUMN exit_order_id TEXT", "trades"),
        # 2026-08-10: add mode to signals for paper/live DB separation
        ("ALTER TABLE signals ADD COLUMN mode TEXT DEFAULT 'live'", "signals"),
    ]

    for sql, table in migrations:
        try:
            cur.execute(sql)
            conn.commit()
            print(f"[DB] Migration applied: {sql}")
        except Exception as e:
            # Column already exists — expected for re-runs
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                print(f"[DB] Migration warning on {table}: {e}")

    conn.close()


def log_signal(signal, status="rejected", rejection_reason=None, trade_id=None):
    """Persist a scan signal to the DB for audit.

    Called by the scanner for every candidate, regardless of whether the
    signal was taken, rejected, or filled. Provides a complete audit trail
    of what the scanner saw and how the executor responded.
    """
    import json as _json
    conn = get_connection()
    # Extract ML features from signal components JSON
    components = signal.get("components", {}) or {}
    comps_str = _json.dumps(components)

    # FIX 2026-08-13: Classify market regime from symbol's recent candles.
    # Best-effort — if candles unavailable or regime module errors, store 0.
    regime_id = 0
    regime_label = 'range_low'
    vol_zscore = 0.0
    trend_slope_pct = 0.0
    realized_vol_pct = 0.0
    try:
        from regime import classify
        sym = signal.get("symbol") or "UNKNOWN"
        try:
            from robinhood_client import get_client
            _client = get_client()
            _client._ensure_logged_in()
            _candles = _client.get_historicals(sym, interval="5minute", span="week")
            if _candles:
                _reg = classify(_candles)
                regime_id = _reg.get("regime_id", 0)
                regime_label = _reg.get("regime_label", "range_low")
                vol_zscore = _reg.get("vol_zscore", 0.0)
                trend_slope_pct = _reg.get("trend_slope_pct", 0.0)
                realized_vol_pct = _reg.get("realized_vol_pct", 0.0)
        except Exception:
            pass  # best-effort; do not block signal logging
    except Exception:
        pass

    # FIX 2026-08-13: signal-sequence context (trades_today, loss streak, etc.)
    seq = {
        "trades_today_for_symbol": 0,
        "trades_today_total": 0,
        "consecutive_loss_streak": 0,
        "consecutive_win_streak": 0,
        "minutes_since_last_loss_global": 99999.0,
        "minutes_since_last_symbol_trade": 99999.0,
        "symbol_prior_trades": 0,
        "symbol_prior_winrate": 0.5,
        "today_pnl": 0.0,
        "recent_5_winrate": 0.5,
        "recent_5_avg_pnl": 0.0,
    }
    try:
        from signal_context import compute
        _sym = signal.get("symbol") or "SPY"
        seq = compute(_sym)
    except Exception:
        pass  # best-effort; do not block signal logging

    conn.execute("""
        INSERT INTO signals (
            timestamp, symbol, timeframe, candles_used, score,
            components, direction, status, rejection_reason, trade_id, mode,
            rsi_value, macd_hist, ema_9, ema_20, vwap, volume_ratio,
            bb_position, prior_day_tod_trend, ml_score, hour_of_day,
            regime_id, regime_label, vol_zscore, trend_slope_pct, realized_vol_pct,
            trades_today_for_symbol, trades_today_total,
            consecutive_loss_streak, consecutive_win_streak,
            minutes_since_last_loss_global, minutes_since_last_symbol_trade,
            symbol_prior_trades, symbol_prior_winrate, today_pnl,
            recent_5_winrate, recent_5_avg_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(ET).isoformat(),
        signal.get("symbol") or "UNKNOWN",
        signal.get("timeframe", "5minute"),
        signal.get("candles_used", 0),
        signal.get("score", 0),
        comps_str,
        signal.get("direction") or "none",
        status,
        rejection_reason,
        trade_id,
        config.MODE,  # 2026-08-10: paper/live DB separation
        # ML features (FIX 2026-08-13 — for learning)
        components.get("rsi"),
        components.get("macd_histogram"),
        components.get("ema_9"),
        components.get("ema_20"),
        components.get("vwap"),
        components.get("volume_ratio"),
        components.get("bb_position"),
        components.get("prior_day_tod_trend"),
        components.get("ml_score"),
        datetime.now(ET).hour,
        # Market regime (FIX 2026-08-13)
        regime_id,
        regime_label,
        vol_zscore,
        trend_slope_pct,
        realized_vol_pct,
        # Signal-sequence context (FIX 2026-08-13)
        seq.get("trades_today_for_symbol", 0),
        seq.get("trades_today_total", 0),
        seq.get("consecutive_loss_streak", 0),
        seq.get("consecutive_win_streak", 0),
        seq.get("minutes_since_last_loss_global", 99999.0),
        seq.get("minutes_since_last_symbol_trade", 99999.0),
        seq.get("symbol_prior_trades", 0),
        seq.get("symbol_prior_winrate", 0.5),
        seq.get("today_pnl", 0.0),
        seq.get("recent_5_winrate", 0.5),
        seq.get("recent_5_avg_pnl", 0.0),
    ))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
