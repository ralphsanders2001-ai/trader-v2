#!/usr/bin/env python3
"""Simple daily P&L ledger.

Two columns only:
  date        daily_pnl    running_total

'daily_pnl' sums all closed trades' pnl on that day.
'running_total' is the cumulative sum of daily_pnl up to and including that date.

Usage:
  scripts/ledger.py                  # rebuild + print
  scripts/ledger.py --rebuild        # recompute every day's row from the trades table
  scripts/ledger.py --print          # print without rebuilding
  scripts/ledger.py --date 2026-07-29 # rebuild + show one day
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
from datetime import date as _date_type
from database import get_db


def rebuild():
    """Recompute every ledger row from the trades table."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM ledger")
        cur.execute("""
            SELECT trade_date, COALESCE(SUM(pnl), 0)
            FROM trades
            WHERE trade_date IS NOT NULL AND trade_date != ''
            GROUP BY trade_date
            ORDER BY trade_date
        """)
        running = 0.0
        for d, pnl in cur.fetchall():
            running += float(pnl or 0)
            cur.execute("""
                INSERT INTO ledger (date, daily_pnl, running_total)
                VALUES (?, ?, ?)
            """, (d, pnl, running))
        conn.commit()


def print_table(target=None):
    with get_db() as conn:
        cur = conn.cursor()
        if target:
            cur.execute(
                "SELECT date, daily_pnl, running_total FROM ledger WHERE date=?",
                (target,)
            )
        else:
            cur.execute("SELECT date, daily_pnl, running_total FROM ledger ORDER BY date")
        rows = cur.fetchall()

    if not rows:
        print("(empty)")
        return

    print(f"{'Date':<12} {'Daily P&L':>12} {'Running Total':>16}")
    print("-" * 42)
    for d, dp, rt in rows:
        sign = "+" if dp >= 0 else ""
        rt_sign = "+" if rt >= 0 else ""
        print(f"{d:<12} {sign}{dp:>10.2f} {rt_sign}{rt:>14.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="Recompute all rows from trades")
    ap.add_argument("--print", action="store_true", help="Print current ledger")
    ap.add_argument("--date", help="Show one specific date (YYYY-MM-DD); triggers rebuild for today")
    args = ap.parse_args()

    if args.date:
        if args.date == "today":
            args.date = _date_type.today().isoformat()
        # Just rebuild today's row by recomputing the day
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COALESCE(SUM(pnl), 0) FROM trades
                WHERE trade_date = ?
            """, (args.date,))
            today_pnl = cur.fetchone()[0] or 0.0
            # Get prior running total
            cur.execute("""
                SELECT running_total FROM ledger
                WHERE date < ? ORDER BY date DESC LIMIT 1
            """, (args.date,))
            prior = cur.fetchone()
            prior_rt = float(prior[0]) if prior else 0.0
            cur.execute("""
                INSERT OR REPLACE INTO ledger (date, daily_pnl, running_total)
                VALUES (?, ?, ?)
            """, (args.date, today_pnl, prior_rt + today_pnl))
            conn.commit()
    elif args.rebuild or not (args.print or args.date):
        rebuild()

    print_table(target=args.date)


if __name__ == "__main__":
    main()
