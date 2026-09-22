#!/usr/bin/env python
"""
CLI script: record a sample paper trade and report performance.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db
from account import AccountManager
from execution import ExecutionEngine
from performance import PerformanceTracker
from signals import momentum_pullback_signal

def main():
    init_db()
    engine = ExecutionEngine(mode="paper")
    pt = PerformanceTracker()

    # Real-data TSLA signal (requires market data available)
    sig = momentum_pullback_signal(symbol="TSLA", side="call")
    print("Signal:", sig)

    result = engine.enter(sig, quantity=1)
    print("Enter result:", result)

    if result.success:
        # Simulate a profitable exit at 4.10 premium (bought at 3.45)
        close = engine.close(result.trade_id, exit_price=4.10, approve_loss=False)
        print("Close result:", close)

    # Also demonstrate the loss-exit approval block
    loss_sig = momentum_pullback_signal(symbol="META", side="call")
    if loss_sig.action == "buy":
        loss_entry = engine.enter(loss_sig)
        if loss_entry.success:
            blocked_close = engine.close(loss_entry.trade_id, exit_price=loss_sig.option_premium * 0.5, approve_loss=False)
            print("Blocked loss exit:", blocked_close)
            approved_close = engine.close(loss_entry.trade_id, exit_price=loss_sig.option_premium * 0.5, approve_loss=True)
            print("Approved loss exit:", approved_close)

    print("Daily summary:", pt.daily_summary())
    print("All time:", pt.all_time())
    print("Account:", AccountManager().get_state())

if __name__ == "__main__":
    main()
