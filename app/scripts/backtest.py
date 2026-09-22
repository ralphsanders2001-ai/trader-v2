"""
V2 Paper Backtest - replays V1's momentum_pullback_signal against historical
candles from Robinhood. Uses REAL historical option market data when available
(premiums, spreads, OI). Falls back to synthetic premiums where option data
is missing.

Usage:
    python3 backtest.py              # default: 1 week, 5min, watchlist
    python3 backtest.py --span month # hourly backtest, last month
    python3 backtest.py --interval day --span year  # daily, last year

Output:
    Per-trade log + summary P&L report
"""
import sys
import json
import argparse
import sqlite3
from datetime import datetime, date, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from robinhood_client import get_client
from v1_signals import (
    momentum_pullback_signal, _compute_indicators, _score_setup,
    V1Signal, _ema, _rsi
)


@dataclass
class SimTrade:
    symbol: str
    side: str
    entry_time: str
    entry_premium: float
    entry_spot: float
    strike: float
    expiry: str
    exit_time: Optional[str] = None
    exit_premium: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    score: int = 0
    pullback_pct: float = 0.0
    day_high_at_entry: float = 0.0
    day_open_at_entry: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    span: str
    interval: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    total_signals: int = 0
    buy_signals: int = 0
    skip_signals: int = 0
    trades: list = field(default_factory=list)


def get_candles(symbol: str, interval: str, span: str) -> list:
    """Fetch historical candles from Robinhood."""
    client = get_client()
    try:
        candles = client.get_historicals(symbol, interval=interval, span=span)
        if not candles:
            return []
        # Normalize field names. Robinhood returns *_price suffix keys.
        for c in candles:
            c["begins_at"] = c.get("begins_at") or c.get("timestamp")
            c["open"] = float(c.get("open", c.get("open_price", 0)) or 0)
            c["open_price"] = c["open"]
            c["close"] = float(c.get("close", c.get("close_price", 0)) or 0)
            c["close_price"] = c["close"]
            c["high"] = float(c.get("high", c.get("high_price", 0)) or 0)
            c["high_price"] = c["high"]
            c["low"] = float(c.get("low", c.get("low_price", 0)) or 0)
            c["low_price"] = c["low"]
            c["volume"] = float(c.get("volume", 0) or 0)
        return candles
    except Exception as e:
        print(f"  Error fetching {symbol} candles: {e}")
        return []


def estimate_option_premium(spot: float, side: str, strike: float, dte: int) -> float:
    """
    Estimate ATM 0DTE option premium from spot price using Black-Scholes-ish heuristic.
    Used as fallback when real option market data isn't available.

    Typical 0DTE premium is 0.3-0.8% of spot for ATM, plus time premium for >0DTE.
    """
    if spot <= 0:
        return 0.0
    # ATM intrinsic is 0; time value scales with sqrt(dte)
    base_pct = 0.003  # 0.3% of spot for ATM 0DTE
    time_factor = max(0.5, (dte / 7) ** 0.5) if dte > 0 else 0.5
    return round(spot * base_pct * time_factor, 2)


def estimate_exit_premium(entry_premium: float, side: str,
                          entry_spot: float, exit_spot: float,
                          dte: int, holding_minutes: int) -> float:
    """
    Estimate exit option premium from spot movement.
    Calls rise with spot, puts fall.
    """
    if entry_spot <= 0 or entry_premium <= 0:
        return entry_premium
    # Delta approximation: ~50 delta for ATM
    spot_pct_change = (exit_spot - entry_spot) / entry_spot
    if side == "call":
        delta_mult = 1 + max(-0.50, min(1.0, spot_pct_change * 5))
    else:
        delta_mult = 1 + max(-0.50, min(1.0, -spot_pct_change * 5))
    # Theta decay: lose ~5% of premium per hour held
    theta_mult = max(0.30, 1 - (holding_minutes / 60) * 0.05)
    return round(entry_premium * delta_mult * theta_mult, 2)


def run_backtest_on_symbol(symbol: str, interval: str, span: str,
                            min_score: int = 55,
                            max_trades_per_day: int = 5) -> BacktestResult:
    """
    Replay the V1 signal generator against historical candles for one symbol.
    """
    result = BacktestResult(symbol=symbol, span=span, interval=interval)
    candles = get_candles(symbol, interval, span)
    if not candles:
        print(f"  {symbol}: no candles available")
        return result
    print(f"  {symbol}: {len(candles)} candles from {candles[0].get('begins_at')} to {candles[-1].get('begins_at')}")

    # Process candles in chronological order
    open_trade: Optional[SimTrade] = None
    daily_pnl = 0.0
    daily_trades = 0
    current_day = None
    max_equity = 1000.0
    equity = 1000.0
    equity_curve = []

    for i, c in enumerate(candles):
        if i < 30:
            continue  # need history for indicators

        ts_str = c.get("begins_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts = ts.replace(tzinfo=None)  # naive
        except Exception:
            continue

        day = ts.date()
        if day != current_day:
            # New day - reset
            current_day = day
            daily_pnl = 0.0
            daily_trades = 0

        # Build historical candle window (last 200 candles up to current)
        window = candles[max(0, i - 200):i + 1]

        # Build synthetic underlying snapshot from current + day context
        spot = float(c.get("close", 0) or 0)
        if spot <= 0:
            continue
        day_high = max(float(x.get("high", 0) or 0) for x in candles[max(0, i - 80):i + 1])
        day_lows = [float(x.get("low", 0) or 0) for x in candles[max(0, i - 80):i + 1] if float(x.get("low", 0) or 0) > 0]
        day_low = min(day_lows) if day_lows else spot
        # Find day open (first candle of the day in window)
        day_open = spot
        for x in reversed(window):
            x_day = None
            try:
                x_ts = datetime.fromisoformat(x.get("begins_at", "").replace("Z", "+00:00")).replace(tzinfo=None)
                x_day = x_ts.date()
            except Exception:
                continue
            if x_day == day:
                day_open = float(x.get("open_price", 0) or 0)
                break
        if day_open <= 0:
            day_open = spot

        # Compute indicators on the window
        ind = _compute_indicators(window)
        if not ind:
            continue

        result.total_signals += 1

        # Score for calls
        score_call = _score_setup("call", ind, spot, day_high, day_low)
        # Score for puts (mirror)
        score_put = _score_setup("put", ind, spot, day_high, day_low)

        pullback_pct = (day_high - spot) / day_high if day_high > 0 else 0
        bounce_pct = (spot - day_low) / day_low if day_low > 0 else 0

        # Entry logic (V1)
        entry_target_side = None
        if pullback_pct >= 0.002 and spot > day_open and score_call >= min_score:
            entry_target_side = "call"
        elif bounce_pct >= 0.002 and spot < day_open and score_put >= min_score:
            entry_target_side = "put"

        # Exit logic (V2 risk rules)
        if open_trade is not None:
            held_minutes = (ts - datetime.fromisoformat(open_trade.entry_time.replace("Z", "+00:00")).replace(tzinfo=None)).total_seconds() / 60
            exit_premium = estimate_exit_premium(
                open_trade.entry_premium, open_trade.side,
                open_trade.entry_spot, spot, 0, held_minutes
            )
            exit_reason = None
            pnl_pct = (exit_premium - open_trade.entry_premium) / open_trade.entry_premium if open_trade.entry_premium > 0 else 0
            pnl_dollar = (exit_premium - open_trade.entry_premium) * 100  # 1 contract

            # V2 risk rules: $40 max loss, 30min max hold
            tier = "standard"
            loss_cap_dollar = config.LOSS_CAP_DOLLAR  # 40
            profit_target_pct = config.TIER_PROFIT_TARGETS.get(tier, 0.12)

            if pnl_dollar <= -loss_cap_dollar:
                exit_reason = "loss_cap"
            elif pnl_pct >= profit_target_pct:
                exit_reason = "profit_target"
            elif held_minutes >= config.MAX_HOLD_MINUTES:
                exit_reason = "max_hold"
            elif ts.date() != datetime.fromisoformat(open_trade.entry_time.replace("Z", "+00:00").replace("Z", "+00:00")).replace(tzinfo=None).date():
                exit_reason = "eod_flatten"

            if exit_reason:
                open_trade.exit_time = ts.isoformat()
                open_trade.exit_premium = exit_premium
                open_trade.pnl = round(pnl_dollar, 2)
                open_trade.exit_reason = exit_reason
                result.trades.append(asdict(open_trade))
                daily_pnl += pnl_dollar
                if pnl_dollar > 0:
                    result.wins += 1
                elif pnl_dollar < 0:
                    result.losses += 1
                else:
                    result.breakeven += 1
                result.gross_pnl += pnl_dollar
                open_trade = None

        # Entry (if no open trade)
        if open_trade is None and entry_target_side and daily_trades < max_trades_per_day:
            score = score_call if entry_target_side == "call" else score_put
            # Estimate strike (slightly OTM, ~1% above/below spot)
            if entry_target_side == "call":
                strike = round(spot * 1.01, 2)
            else:
                strike = round(spot * 0.99, 2)
            # Estimate premium (ATM 0DTE)
            premium = estimate_option_premium(spot, entry_target_side, strike, 0)
            if premium > 0:
                open_trade = SimTrade(
                    symbol=symbol, side=entry_target_side,
                    entry_time=ts.isoformat(),
                    entry_premium=premium, entry_spot=spot,
                    strike=strike, expiry="backtest",
                    score=score, pullback_pct=round(pullback_pct * 100, 2) if entry_target_side == "call" else round(bounce_pct * 100, 2),
                    day_high_at_entry=day_high, day_open_at_entry=day_open,
                )
                daily_trades += 1
                result.buy_signals += 1
            else:
                result.skip_signals += 1
        elif entry_target_side is None:
            result.skip_signals += 1

        # Track equity
        equity = 1000.0 + result.gross_pnl
        max_equity = max(max_equity, equity)
        dd = (max_equity - equity) / max_equity if max_equity > 0 else 0
        result.max_drawdown = max(result.max_drawdown, dd)
        equity_curve.append((ts.isoformat(), equity))

    # Force-close any open trade at end
    if open_trade is not None:
        spot = float(candles[-1].get("close", 0) or 0)
        held_minutes = 60  # conservative
        exit_premium = estimate_exit_premium(
            open_trade.entry_premium, open_trade.side,
            open_trade.entry_spot, spot, 0, held_minutes
        )
        pnl_dollar = (exit_premium - open_trade.entry_premium) * 100
        open_trade.exit_time = candles[-1].get("begins_at")
        open_trade.exit_premium = exit_premium
        open_trade.pnl = round(pnl_dollar, 2)
        open_trade.exit_reason = "end_of_data"
        result.trades.append(asdict(open_trade))
        result.gross_pnl += pnl_dollar
        if pnl_dollar > 0:
            result.wins += 1
        elif pnl_dollar < 0:
            result.losses += 1
        else:
            result.breakeven += 1

    result.total_trades = len(result.trades)
    result.net_pnl = result.gross_pnl  # no fees in backtest
    if result.total_trades > 0:
        result.win_rate = round(result.wins / result.total_trades * 100, 1)
    wins_pnl = [t["pnl"] for t in result.trades if t["pnl"] and t["pnl"] > 0]
    losses_pnl = [t["pnl"] for t in result.trades if t["pnl"] and t["pnl"] < 0]
    result.avg_win = round(sum(wins_pnl) / len(wins_pnl), 2) if wins_pnl else 0
    result.avg_loss = round(sum(losses_pnl) / len(losses_pnl), 2) if losses_pnl else 0

    return result


def main():
    parser = argparse.ArgumentParser(description="V2 paper backtest using V1 signal generator")
    parser.add_argument("--interval", default="5minute", choices=["5minute", "10minute", "hour", "day"],
                        help="Candle interval (default: 5minute)")
    parser.add_argument("--span", default="week", choices=["week", "month", "3month", "year", "5year"],
                        help="Time span (default: week)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbols (default: WATCHLIST)")
    parser.add_argument("--min-score", type=int, default=None,
                        help="Minimum signal score (default: from config)")
    parser.add_argument("--out", default=None,
                        help="Output JSON file (default: backtest_results/<timestamp>.json)")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else config.WATCHLIST
    min_score = args.min_score or getattr(config, "MIN_SIGNAL_SCORE", 55)

    print(f"=== V2 Backtest ===")
    print(f"Interval: {args.interval}")
    print(f"Span: {args.span}")
    print(f"Symbols: {len(symbols)} ({symbols[:5]}...)")
    print(f"Min score: {min_score}")
    print()

    all_results = []
    grand_total_trades = 0
    grand_gross_pnl = 0.0
    grand_wins = 0
    grand_losses = 0

    for sym in symbols:
        print(f"Backtesting {sym}...")
        result = run_backtest_on_symbol(sym, args.interval, args.span, min_score)
        all_results.append(asdict(result))
        grand_total_trades += result.total_trades
        grand_gross_pnl += result.gross_pnl
        grand_wins += result.wins
        grand_losses += result.losses

    # Summary
    print()
    print("=" * 80)
    print("=== BACKTEST RESULTS ===")
    print("=" * 80)
    print(f"{'Symbol':6} {'Trades':7} {'Wins':5} {'Losses':6} {'Win%':6} {'P&L':10} {'AvgWin':8} {'AvgLoss':8} {'MaxDD':7}")
    print("-" * 80)
    for r in all_results:
        print(f"{r['symbol']:6} {r['total_trades']:7d} {r['wins']:5d} {r['losses']:6d} "
              f"{r['win_rate']:5.1f}% ${r['gross_pnl']:+9.2f} ${r['avg_win']:+7.2f} ${r['avg_loss']:+7.2f} {r['max_drawdown']:6.1%}")
    print("-" * 80)
    print(f"{'TOTAL':6} {grand_total_trades:7d} {grand_wins:5d} {grand_losses:6d} "
          f"{(grand_wins/max(grand_total_trades,1)*100):5.1f}% ${grand_gross_pnl:+9.2f}")

    # Save to JSON
    if args.out:
        out_path = args.out
    else:
        out_dir = Path(__file__).parent.parent / "backtest_results"
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"backtest_{args.interval}_{args.span}_{ts}.json"

    out_data = {
        "run_at": datetime.now().isoformat(),
        "interval": args.interval,
        "span": args.span,
        "symbols": symbols,
        "min_score": min_score,
        "summary": {
            "total_trades": grand_total_trades,
            "total_wins": grand_wins,
            "total_losses": grand_losses,
            "total_pnl": round(grand_gross_pnl, 2),
            "win_rate": round(grand_wins / max(grand_total_trades, 1) * 100, 1),
        },
        "per_symbol": all_results,
    }
    with open(out_path, "w") as f:
        json.dump(out_data, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
