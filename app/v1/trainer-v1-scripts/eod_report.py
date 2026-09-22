"""
End-of-day report generator for the trainer.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db
from account import AccountManager
from performance import PerformanceTracker
from datetime import datetime, timezone
import argparse

def eod_report(mode: str = "paper"):
    init_db()
    state = AccountManager().get_state(mode=mode)
    pt = PerformanceTracker()
    today = datetime.now(timezone.utc).date().isoformat()
    daily = pt.daily_summary(today, mode)
    all_time = pt.all_time(mode)

    print("=== End of Day Report ===")
    print(f"Date: {today}")
    print(f"Mode: {mode}")
    print(f"Cash: {state.cash:.2f} | BP: {state.buying_power:.2f} | Equity: {state.equity:.2f}")
    print(f"Day trades today: {state.buy_count}")
    print(f"Today's PnL: {daily['net_pnl']:.2f} ({daily['wins']} wins, {daily['losses']} losses)")
    print(f"All-time PnL: {all_time['net_pnl']:.2f} over {all_time['closed_trades']} trades, win rate {all_time['win_rate']}%")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    args = parser.parse_args()
    eod_report(mode=args.mode)
