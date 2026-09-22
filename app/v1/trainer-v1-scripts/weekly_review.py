"""
Weekly performance review: compare paper vs live and suggest signal improvements.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db, get_db
from datetime import datetime, timedelta, timezone
from performance import PerformanceTracker

def weekly_review():
    init_db()
    pt = PerformanceTracker()
    today = datetime.now(timezone.utc).date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    today_str = today.isoformat()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT mode, COUNT(*) as trades, SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END) as gross, SUM(CASE WHEN status='closed' THEN fees ELSE 0 END) as fees, SUM(CASE WHEN pnl > 0 AND status='closed' THEN 1 ELSE 0 END) as wins FROM trades WHERE trade_date >= ? AND trade_date <= ? GROUP BY mode",
            (week_start, today_str),
        ).fetchall()

    print(f"=== Weekly Review {week_start} to {today_str} ===")
    for row in rows:
        r = dict(row)
        net = r["gross"] - r["fees"]
        win_rate = round(r["wins"] / r["trades"] * 100, 1) if r["trades"] else 0
        print(f"Mode {r['mode']}: {r['trades']} trades, gross {r['gross']:.2f}, fees {r['fees']:.2f}, net {net:.2f}, win rate {win_rate}%")

if __name__ == "__main__":
    weekly_review()
