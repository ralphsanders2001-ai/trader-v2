#!/usr/bin/env python
"""
CLI script: run a simple paper backtest over a few sample price scenarios.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db
from execution import ExecutionEngine
from performance import PerformanceTracker
from signals import momentum_pullback_signal

SCENARIOS = [
    ("TSLA", 408.19, 406.55, 412.58, 404.91, 410.0, 3.45, "2026-07-10", "call", 4.10),
    ("QQQ",  721.39, 720.70, 724.04, 719.83, 723.0, 1.71, "2026-07-10", "call", 2.10),
    ("META", 664.47, 661.26, 677.85, 658.01, 670.0, 5.20, "2026-07-10", "call", 4.50),
]

def main():
    init_db()
    engine = ExecutionEngine(mode="paper")
    pt = PerformanceTracker()

    scenarios = [
        ("TSLA", "call"),
        ("QQQ", "call"),
        ("META", "call"),
    ]
    for symbol, side in scenarios:
        sig = momentum_pullback_signal(symbol, side)
        print(f"{symbol}: {sig.action} - {sig.reason}")
        if sig.action == "buy":
            res = engine.enter(sig, quantity=1)
            # simulate a 20% premium gain
            if res.success:
                exit_premium = round(sig.option_premium * 1.20, 2)
                engine.close(res.trade_id, exit_premium)

    print("Daily summary:", pt.daily_summary())
    print("All time:", pt.all_time())

if __name__ == "__main__":
    main()
