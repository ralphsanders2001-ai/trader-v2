"""
Historical backtest CLI.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from historical_backtest import HistoricalBacktest

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--start", default="2026-06-01")
    parser.add_argument("--end", default="2026-07-11")
    parser.add_argument("--side", default="call", choices=["call", "put"])
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--cash", type=float, default=1000.0)
    parser.add_argument("--min-score", type=int, default=55)
    args = parser.parse_args()

    bt = HistoricalBacktest(args.symbol, args.start, args.end, args.interval, args.cash)
    res = bt.run(side=args.side, min_score=args.min_score)
    print(f"Symbol: {args.symbol} | Side: {args.side} | Period: {args.start} to {args.end}")
    print(f"Total trades: {res.total_trades}")
    print(f"Wins: {res.wins} | Losses: {res.losses}")
    print(f"Gross PnL: {res.gross_pnl:.2f}")
    print(f"Net PnL: {res.net_pnl:.2f}")
    print(f"Win rate: {res.win_rate}%")
    print(f"Max drawdown: {res.max_drawdown}%")
    print(f"Final equity: {res.equity_curve[-1][1]:.2f}")

if __name__ == "__main__":
    main()
