"""
Daily scan: evaluate base watchlist + top traded stocks and log top signals.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db
from account import AccountManager
from signals import momentum_pullback_signal
import config
from universe import get_scan_symbols

def daily_scan(mode: str = None):
    init_db()
    state = AccountManager().get_state()
    mode = mode or state.mode

    if state.mode not in (config.Mode.PAPER, config.Mode.LIVE):
        print(f"Account mode is {state.mode}, skipping scan")
        return

    symbols = get_scan_symbols()
    print(f"Mode: {mode} | BP: {state.buying_power} | Buy count: {state.buy_count} | Scanning {len(symbols)} symbols")

    candidates = []
    for symbol in symbols:
        for side in ("call", "put"):
            sig = momentum_pullback_signal(symbol, side=side)
            print(f"{symbol} {side}: {sig.action} score={sig.score} {sig.reason}")
            if sig.action == "buy":
                candidates.append(sig)

    candidates.sort(key=lambda s: -s.score)
    print(f"\nTop {len(candidates)} buy candidates:")
    for c in candidates[:3]:
        print(f"  {c.symbol} {c.side} strike={c.strike} expiry={c.expiry} premium={c.option_premium} score={c.score}")
        if c.score >= 75:
            print(f"  ALERT: strong candidate {c.symbol} {c.side} score={c.score}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default=None)
    args = parser.parse_args()
    daily_scan(args.mode)
