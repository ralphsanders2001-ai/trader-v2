#!/usr/bin/env python3
"""Daily signal archive sync to NAS.

Runs at end-of-day via cron. Mirrors the day's signals + trades to
/mnt/file-cabinet/trader-archive/ so we never lose training data.

Idempotent: safe to run multiple times.
"""
import sqlite3
import shutil
import os
from datetime import datetime, date

DB = '/home/ralph/trader-v2/data/trader_paper.db'
ARCHIVE_SIGNALS = '/home/ralph/trader-v2/data/signal_archive/signals_archive.db'
NAS_SIGNALS = '/mnt/file-cabinet/trader-archive/signals'
NAS_TRADES = '/mnt/file-cabinet/trader-archive/trades'

today = date.today().isoformat()

# 1. Copy day's signals to local archive
primary = sqlite3.connect(DB)
archive = sqlite3.connect(ARCHIVE_SIGNALS)

today_signals = primary.execute("""
    SELECT id, timestamp, symbol, score, direction, status, rejection_reason,
           rsi_value, macd_hist, ema_9, ema_20, vwap, volume_ratio,
           bb_position, vix, spread_pct, hour_of_day, prior_day_tod_trend,
           ml_score, pnl_at_exit, hold_minutes, outcome
    FROM signals WHERE DATE(timestamp)=?
""", (today,)).fetchall()

cols = ['id', 'timestamp', 'symbol', 'score', 'direction', 'status', 'rejection_reason',
        'rsi_value', 'macd_hist', 'ema_9', 'ema_20', 'vwap', 'volume_ratio',
        'bb_position', 'vix', 'spread_pct', 'hour_of_day', 'prior_day_tod_trend',
        'ml_score', 'pnl_at_exit', 'hold_minutes', 'outcome']

# Skip already-archived
existing = set(r[0] for r in archive.execute("SELECT id FROM signals_archive").fetchall())
new_rows = [r for r in today_signals if r[0] not in existing]

if new_rows:
    placeholders = ','.join(['?'] * len(cols))
    archive.executemany(
        f'INSERT INTO signals_archive ({",".join(cols)}) VALUES ({placeholders})',
        new_rows,
    )
    archive.commit()
    print(f'Archived {len(new_rows)} new signals to local archive')

# 2. Sync archive DB to NAS
os.makedirs(NAS_SIGNALS, exist_ok=True)
shutil.copy2(ARCHIVE_SIGNALS, f'{NAS_SIGNALS}/signals_archive_{today}.db')
shutil.copy2(ARCHIVE_SIGNALS, f'{NAS_SIGNALS}/signals_archive_latest.db')

# 3. Export today's trades to CSV on NAS
trade_csv = f'{NAS_TRADES}/trades_{today}.csv'
today_trades = primary.execute("""
    SELECT id, timestamp_open, timestamp_close, symbol, option_strike,
           option_expiry, option_type, entry_price, exit_price, pnl, exit_reason
    FROM trades WHERE DATE(timestamp_close)=?
""", (today,)).fetchall()

with open(trade_csv, 'w') as f:
    f.write('id,timestamp_open,timestamp_close,symbol,option_strike,option_expiry,option_type,entry_price,exit_price,pnl,exit_reason\n')
    for row in today_trades:
        f.write(','.join(str(v) if v is not None else '' for v in row) + '\n')

print(f'Wrote {trade_csv} ({len(today_trades)} trades)')

# 4. Latest cumulative file for ML training
latest_csv = f'{NAS_TRADES}/all_trades.csv'
if not os.path.exists(latest_csv):
    shutil.copy2(trade_csv, latest_csv)

primary.close()
archive.close()
print(f'Archive sync complete for {today}')