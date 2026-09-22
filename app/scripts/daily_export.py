"""
Daily Trade Data Saver

Runs at 4:10 PM ET (after shutdown cron at 4:05 PM).
Exports today's trade data + signals to:
- Local: /home/ralph/trader-v2/trade-data/
- NAS: /mnt/file-cabinet/trader-v2/trade-data/

Keeps accumulating the database over time so we have a growing dataset
for retraining. No compression/deletion — just append.
"""
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
LOCAL_TRADE_DATA = Path("/home/ralph/trader-v2/trade-data")
BACKUP_DRIVE_DATA = Path("/mnt/backup-mount/trader-v2/trade-data")
NAS_TRADE_DATA = Path("/mnt/file-cabinet/trader-v2/trade-data")


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"Daily trade export — {today}")

    LOCAL_TRADE_DATA.mkdir(parents=True, exist_ok=True)
    BACKUP_DRIVE_DATA.mkdir(parents=True, exist_ok=True)
    NAS_TRADE_DATA.mkdir(parents=True, exist_ok=True)
    (NAS_TRADE_DATA / "daily").mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    # Export today's trades
    trades = pd.read_sql_query("""
        SELECT * FROM trades WHERE DATE(timestamp_open) = ?
    """, conn, params=[today])

    # Export today's signals
    signals = pd.read_sql_query("""
        SELECT * FROM signals WHERE DATE(timestamp) = ?
    """, conn, params=[today])

    # Export full DB snapshot (for completeness)
    all_trades = pd.read_sql_query("SELECT * FROM trades", conn)
    all_signals = pd.read_sql_query("SELECT * FROM signals", conn)

    conn.close()

    # Save to local
    trades.to_csv(LOCAL_TRADE_DATA / f"trades-{today}.csv", index=False)
    signals.to_csv(LOCAL_TRADE_DATA / f"signals-{today}.csv", index=False)

    # Save to backup drive (4TB local drive - primary training data store)
    trades.to_csv(BACKUP_DRIVE_DATA / f"trades-{today}.csv", index=False)
    signals.to_csv(BACKUP_DRIVE_DATA / f"signals-{today}.csv", index=False)

    # Save to NAS
    trades.to_csv(NAS_TRADE_DATA / "daily" / f"trades-{today}.csv", index=False)
    signals.to_csv(NAS_TRADE_DATA / "daily" / f"signals-{today}.csv", index=False)

    # Cumulative full DB snapshot
    all_trades.to_csv(BACKUP_DRIVE_DATA / "all-trades-cumulative.csv", index=False)
    all_signals.to_csv(BACKUP_DRIVE_DATA / "all-signals-cumulative.csv", index=False)
    all_trades.to_csv(NAS_TRADE_DATA / "all-trades-cumulative.csv", index=False)
    all_signals.to_csv(NAS_TRADE_DATA / "all-signals-cumulative.csv", index=False)

    # Manifest entry
    manifest = {
        "date": today,
        "exported_at": datetime.now().isoformat(),
        "trades_today": len(trades),
        "signals_today": len(signals),
        "trades_cumulative": len(all_trades),
        "signals_cumulative": len(all_signals),
    }

    manifest_file = NAS_TRADE_DATA / "manifest.json"
    if manifest_file.exists():
        with open(manifest_file) as f:
            history = json.load(f)
    else:
        history = {"exports": []}

    history["exports"].append(manifest)
    history["last_updated"] = datetime.now().isoformat()

    with open(manifest_file, 'w') as f:
        json.dump(history, f, indent=2)

    print(f"  Trades today: {len(trades)}")
    print(f"  Signals today: {len(signals)}")
    print(f"  Cumulative trades: {len(all_trades)}")
    print(f"  Cumulative signals: {len(all_signals)}")
    print(f"  Saved to NAS: {NAS_TRADE_DATA}")


if __name__ == "__main__":
    main()