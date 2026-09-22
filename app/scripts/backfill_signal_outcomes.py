#!/usr/bin/env python3
"""Backfill: link each existing signal to its trade outcome (win/loss/open).

For every signal in trader_paper.db that links to a trade (via trade_id),
compute and store pnl_at_exit, hold_minutes, and outcome string.

This is a one-time backfill. After this, the closure-of-trade code path
keeps it current.
"""
import sqlite3
import json

DB = '/home/ralph/trader-v2/data/trader_paper.db'

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Get all signals with their trade
rows = cur.execute("""
    SELECT s.id, s.trade_id, s.timestamp, t.timestamp_open, t.timestamp_close, t.pnl
    FROM signals s
    LEFT JOIN trades t ON t.id = s.trade_id
    WHERE s.trade_id IS NOT NULL
""").fetchall()

updated = 0
for sig_id, trade_id, sig_ts, open_ts, close_ts, pnl in rows:
    if not close_ts or pnl is None:
        outcome = 'open'
        pnl_val = None
        hold_min = None
    else:
        outcome = 'win' if pnl > 0 else 'loss'
        pnl_val = pnl
        # Hold minutes between open and close
        try:
            from datetime import datetime
            o = datetime.fromisoformat(open_ts)
            c = datetime.fromisoformat(close_ts)
            hold_min = int((c - o).total_seconds() / 60)
        except Exception:
            hold_min = None

    cur.execute(
        "UPDATE signals SET pnl_at_exit=?, hold_minutes=?, outcome=? WHERE id=?",
        (pnl_val, hold_min, outcome, sig_id),
    )
    updated += 1

conn.commit()
print(f'Updated {updated} signals with outcome data')

# Summary
counts = cur.execute("""
    SELECT outcome, COUNT(*) FROM signals
    WHERE outcome IS NOT NULL
    GROUP BY outcome
""").fetchall()
print('Outcome breakdown:')
for outcome, count in counts:
    print(f'  {outcome}: {count}')

conn.close()