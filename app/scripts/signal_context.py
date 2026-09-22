"""Compute signal-sequence context features for the v2 trader.

Per the research, the most valuable "what to log" items are:

- trades_today_for_symbol / trades_today_total — Nth trade of the day
- consecutive_loss_streak / consecutive_win_streak — momentum/reversal
- minutes_since_last_loss_global — recency of pain
- minutes_since_last_symbol_trade — cooldown timing
- symbol_prior_winrate / symbol_prior_trades — symbol-specific edge
- today_pnl — running day P&L
- recent_5_winrate / recent_5_avg_pnl — short-term performance

Inputs: paper DB connection + the symbol being scanned + current time.
Output: dict of features with safe defaults if no history yet.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ET = timezone(timedelta(hours=-4))
PAPER_DB = Path('/home/ralph/trader-v2/data/trader_paper.db')


def _open_paper_conn() -> sqlite3.Connection:
    return sqlite3.connect(PAPER_DB)


def _parse_et(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp to ET datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET)
    except Exception:
        return None


def compute(symbol: str, now: Optional[datetime] = None) -> dict:
    """Compute all signal-sequence context features for a symbol at scan time.

    Args:
        symbol: ticker (e.g. 'SPY')
        now: override current time (mostly for testing)

    Returns:
        dict with 11 features, all safe defaults if no history.
    """
    if now is None:
        now = datetime.now(ET)
    today = now.date()

    out = {
        'trades_today_for_symbol': 0,
        'trades_today_total': 0,
        'consecutive_loss_streak': 0,
        'consecutive_win_streak': 0,
        'minutes_since_last_loss_global': 99999.0,
        'minutes_since_last_symbol_trade': 99999.0,
        'symbol_prior_trades': 0,
        'symbol_prior_winrate': 0.5,
        'today_pnl': 0.0,
        'recent_5_winrate': 0.5,
        'recent_5_avg_pnl': 0.0,
    }

    try:
        conn = _open_paper_conn()
        try:
            # All closed paper trades, ordered chronologically
            rows = conn.execute("""
                SELECT timestamp_close, symbol, pnl
                FROM trades
                WHERE timestamp_close IS NOT NULL
                  AND pnl IS NOT NULL
                ORDER BY timestamp_close
            """, ).fetchall()
        finally:
            conn.close()
    except Exception:
        return out

    closed_times = []
    today_trades_all = []
    today_trades_symbol = []
    last_loss_time = None
    last_symbol_trade_time = None
    streak_loss = 0
    streak_win = 0
    today_pnl = 0.0
    sym_trades_history = []
    recent_5 = []

    for ts_str, sym, pnl in rows:
        ts = _parse_et(ts_str)
        if ts is None:
            continue
        if ts.date() == today:
            today_trades_all.append((ts, sym, pnl))
            if sym == symbol:
                today_trades_symbol.append((ts, pnl))
            today_pnl += float(pnl or 0)
        if sym == symbol:
            sym_trades_history.append((ts, pnl))
        closed_times.append((ts, sym, pnl))
        if pnl is None:
            continue
        if (pnl or 0) < 0:
            last_loss_time = ts
            streak_loss += 1
            streak_win = 0
        else:
            streak_win += 1
            streak_loss = 0

    # Today's counts
    out['trades_today_total'] = len(today_trades_all)
    out['trades_today_for_symbol'] = len(today_trades_symbol)
    out['today_pnl'] = round(today_pnl, 2)

    # Streaks — based on most recent trades globally
    if closed_times:
        last_ts, last_sym, last_pnl = closed_times[-1]
        if (last_pnl or 0) < 0:
            out['consecutive_loss_streak'] = streak_loss
            out['consecutive_win_streak'] = 0
        else:
            out['consecutive_win_streak'] = streak_win
            out['consecutive_loss_streak'] = 0

        if last_loss_time:
            minutes = (now - last_loss_time).total_seconds() / 60
            out['minutes_since_last_loss_global'] = round(min(max(minutes, 0), 99999), 2)

    # Last symbol trade time
    sym_closed = [(t, p) for t, s, p in closed_times if s == symbol]
    if sym_closed:
        last_t, _ = sym_closed[-1]
        minutes = (now - last_t).total_seconds() / 60
        out['minutes_since_last_symbol_trade'] = round(min(max(minutes, 0), 99999), 2)

    # Symbol prior stats (excluding today)
    sym_history_excl_today = [(t, p) for t, p in sym_trades_history if t.date() != today]
    if sym_history_excl_today:
        wins = sum(1 for _, p in sym_history_excl_today if (p or 0) > 0)
        out['symbol_prior_trades'] = len(sym_history_excl_today)
        out['symbol_prior_winrate'] = round(wins / len(sym_history_excl_today), 4)

    # Recent 5 global trades
    recent = closed_times[-5:]
    if recent:
        wins = sum(1 for _, _, p in recent if (p or 0) > 0)
        out['recent_5_winrate'] = round(wins / len(recent), 4)
        out['recent_5_avg_pnl'] = round(sum((p or 0) for _, _, p in recent) / len(recent), 2)

    return out


if __name__ == '__main__':
    print('=== Sequence context for SPY right now ===')
    print(compute('SPY'))
    print('\n=== Sequence context for QQQ ===')
    print(compute('QQQ'))
    print('\n=== Sequence context for nonexistent NVDA ===')
    print(compute('NVDA'))