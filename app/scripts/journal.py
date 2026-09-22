"""
Trading Journal & Weekly Review (per article discipline).

Adds:
1. Journal table in database — rationale + emotion + outcome
2. Weekly review tool — analyzes P&L by symbol, time, side, score

Usage:
    from journal import add_journal_entry, weekly_review
    add_journal_entry(trade_id=123, rationale="V1 signal strength", emotion="calm")
    print(weekly_review())
"""
import sys
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/home/ralph/trader-v2')
sys.path.insert(0, '/home/ralph/trader-v2/scripts')
import config
from database import get_connection

log = logging.getLogger("v2.journal")


def init_journal_table():
    """Add journal table if it doesn't exist."""
    conn = get_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            rationale TEXT,
            emotion TEXT,
            market_context TEXT,
            lessons_learned TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def add_journal_entry(trade_id: int, rationale: str = None,
                      emotion: str = None, market_context: str = None,
                      lessons_learned: str = None):
    """Add a journal entry for a trade."""
    init_journal_table()
    conn = get_connection()
    conn.execute('''
        INSERT INTO trade_journal (trade_id, rationale, emotion, market_context, lessons_learned)
        VALUES (?, ?, ?, ?, ?)
    ''', (trade_id, rationale, emotion, market_context, lessons_learned))
    conn.commit()
    conn.close()


def weekly_review(days: int = 7) -> dict:
    """
    Generate a weekly review of trading performance.
    Returns dict with stats and breakdown.
    """
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    # Get all closed trades in the window
    trades = conn.execute('''
        SELECT id, symbol, option_type, option_strike, option_expiry,
               entry_price, exit_price, pnl, exit_reason,
               entry_signal_score, mode, timestamp_open, timestamp_close
        FROM trades
        WHERE timestamp_close IS NOT NULL
          AND timestamp_close > ?
        ORDER BY timestamp_close DESC
    ''', (cutoff,)).fetchall()

    if not trades:
        return {"period_days": days, "total_trades": 0, "message": "No closed trades in window"}

    # Per-symbol breakdown
    by_symbol = {}
    for t in trades:
        sym = t[1]
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0}
        by_symbol[sym]["trades"] += 1
        if t[7] > 0:
            by_symbol[sym]["wins"] += 1
        else:
            by_symbol[sym]["losses"] += 1
        by_symbol[sym]["pnl"] += t[7]

    # Per-side breakdown
    by_side = {"call": {"trades": 0, "wins": 0, "pnl": 0}, "put": {"trades": 0, "wins": 0, "pnl": 0}}
    for t in trades:
        side = t[2]
        if side in by_side:
            by_side[side]["trades"] += 1
            if t[7] > 0:
                by_side[side]["wins"] += 1
            by_side[side]["pnl"] += t[7]

    # Per-exit-reason
    by_reason = {}
    for t in trades:
        reason = t[8] or "unknown"
        if reason not in by_reason:
            by_reason[reason] = {"count": 0, "pnl": 0}
        by_reason[reason]["count"] += 1
        by_reason[reason]["pnl"] += t[7]

    # Per-mode (live vs paper)
    by_mode = {}
    for t in trades:
        mode = t[9] or "unknown"
        if mode not in by_mode:
            by_mode[mode] = {"count": 0, "pnl": 0}
        by_mode[mode]["count"] += 1
        by_mode[mode]["pnl"] += t[7]

    # Score-based breakdown
    score_buckets = {
        "60-69": {"count": 0, "pnl": 0},
        "70-79": {"count": 0, "pnl": 0},
        "80+":   {"count": 0, "pnl": 0},
    }
    for t in trades:
        score = t[10] or 0
        if score is not None and isinstance(score, (int, float)) and 60 <= score < 70:
            bucket = "60-69"
        elif score is not None and isinstance(score, (int, float)) and 70 <= score < 80:
            bucket = "70-79"
        elif score is not None and isinstance(score, (int, float)) and score >= 80:
            bucket = "80+"
        else:
            continue
        score_buckets[bucket]["count"] += 1
        score_buckets[bucket]["pnl"] += t[7]

    total_pnl = sum(t[7] for t in trades)
    winners = sum(1 for t in trades if t[7] > 0)
    losers = sum(1 for t in trades if t[7] < 0)
    win_rate = winners / len(trades) * 100 if trades else 0

    conn.close()

    return {
        "period_days": days,
        "total_trades": len(trades),
        "total_pnl": round(total_pnl, 2),
        "winners": winners,
        "losers": losers,
        "win_rate": round(win_rate, 1),
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_exit_reason": by_reason,
        "by_mode": by_mode,
        "by_score": score_buckets,
    }


def print_review(days: int = 7):
    """Print a formatted weekly review."""
    review = weekly_review(days)
    if review.get("total_trades", 0) == 0:
        print(f"No trades in last {days} days")
        return

    print(f"\n{'='*60}")
    print(f"  WEEKLY TRADING REVIEW — Last {days} days")
    print(f"{'='*60}")
    print(f"  Total trades: {review['total_trades']}")
    print(f"  Total P&L:    ${review['total_pnl']:+.2f}")
    print(f"  Win rate:     {review['win_rate']}% ({review['winners']}W / {review['losers']}L)")
    print()

    print(f"  BY SYMBOL:")
    for sym, stats in sorted(review['by_symbol'].items(), key=lambda x: -x[1]['pnl']):
        wr = stats['wins'] / stats['trades'] * 100 if stats['trades'] else 0
        print(f"    {sym:5} {stats['trades']:>3} trades  {wr:>5.1f}% WR  ${stats['pnl']:+.0f}")
    print()

    print(f"  BY SIDE:")
    for side, stats in review['by_side'].items():
        if stats['trades']:
            wr = stats['wins'] / stats['trades'] * 100
            print(f"    {side:5} {stats['trades']:>3} trades  {wr:>5.1f}% WR  ${stats['pnl']:+.0f}")
    print()

    print(f"  BY EXIT REASON:")
    for reason, stats in sorted(review['by_exit_reason'].items(), key=lambda x: -x[1]['count']):
        print(f"    {reason:25} {stats['count']:>3}  ${stats['pnl']:+.0f}")
    print()

    print(f"  BY ENTRY SCORE:")
    for bucket, stats in review['by_score'].items():
        if stats['count']:
            print(f"    Score {bucket:5} {stats['count']:>3} trades  ${stats['pnl']:+.0f}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    print_review(days=7)
