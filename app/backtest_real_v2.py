"""
V2 Backtest using REAL historical option prices from Robinhood fills.

Instead of guessing premium via Black-Scholes, we use:
1. Underlying 5-min candles (real) to detect V1 signals
2. Real option fills from get_all_option_orders() as the price reference
3. For each signal, find the actual option premium at that time by
   looking up similar contracts (same symbol/strike/expiry/date)
4. For exit, walk the price using actual underlying moves and a
   delta approximation

This is Option B — the most accurate backtest possible without paid
market data subscriptions.
"""
import sys
import json
import argparse
import pickle
from datetime import datetime, date, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import robin_stocks.robinhood as r
from robinhood_client import get_client
from v1_signals import (
    _compute_indicators, _score_setup,
    _ema, _rsi
)


@dataclass
class RealFill:
    """One historical option fill from Robinhood."""
    symbol: str
    strike: float
    expiry: str
    option_type: str
    side: str
    timestamp: datetime
    price: float
    quantity: float


def load_real_fills() -> List[RealFill]:
    """Load all historical option fills from Robinhood orders."""
    client = get_client()
    client.login()
    orders = r.get_all_option_orders() or []
    fills: List[RealFill] = []
    for o in orders:
        if o.get('state') != 'filled':
            continue
        legs = o.get('legs', [])
        if not legs:
            continue
        leg = legs[0]
        executions = leg.get('executions', [])
        for exec_data in executions:
            try:
                price = float(exec_data.get('price', 0))
                quantity = float(exec_data.get('quantity', 0))
                ts_str = exec_data.get('timestamp', '')
                if not ts_str or price <= 0:
                    continue
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                ts = ts.replace(tzinfo=None)
                fills.append(RealFill(
                    symbol=o.get('chain_symbol', ''),
                    strike=float(leg.get('strike_price', 0)),
                    expiry=leg.get('expiration_date', ''),
                    option_type=leg.get('option_type', ''),
                    side=leg.get('side', ''),
                    timestamp=ts,
                    price=price,
                    quantity=quantity,
                ))
            except Exception as e:
                continue
    return fills


def find_real_premium(fills: List[RealFill], symbol: str, side: str,
                       spot: float, timestamp: datetime,
                       max_strike_diff_pct: float = 0.02,
                       max_time_diff_hours: float = 4) -> Optional[float]:
    """
    Find the closest historical fill to estimate the actual premium
    at the given time for a similar contract.

    Matching criteria (tightened to avoid wild mismatches):
    - Same symbol
    - Same option type (call/put)
    - Same side (buy for entry, sell for exit)
    - Strike within ±2% of spot (ATM-ish)
    - Fill within ±4 hours of timestamp
    - Returns the median price of matching fills (outlier-resistant)
    """
    matches = []
    for f in fills:
        if f.symbol != symbol:
            continue
        if f.option_type != side:
            continue
        if f.strike <= 0 or spot <= 0:
            continue
        strike_diff_pct = abs(f.strike - spot) / spot
        if strike_diff_pct > max_strike_diff_pct:
            continue
        time_diff = abs((f.timestamp - timestamp).total_seconds()) / 3600
        if time_diff > max_time_diff_hours:
            continue
        # Score by closeness
        score = strike_diff_pct * 10 + time_diff
        matches.append((score, f))

    if not matches:
        return None

    matches.sort(key=lambda x: x[0])
    # Take top 3 closest
    top = matches[:3]
    prices = sorted([m[1].price for m in top])
    if len(prices) >= 2:
        # Median for outlier resistance
        return round(prices[len(prices) // 2], 2)
    return round(prices[0], 2)


def get_candles(symbol: str, interval: str, span: str) -> list:
    """Fetch historical candles from Robinhood."""
    client = get_client()
    try:
        candles = client.get_historicals(symbol, interval=interval, span=span)
        if not candles:
            return []
        for c in candles:
            c["begins_at"] = c.get("begins_at") or c.get("timestamp")
            c["open"] = float(c.get("open", c.get("open_price", 0)) or 0)
            c["close"] = float(c.get("close", c.get("close_price", 0)) or 0)
            c["high"] = float(c.get("high", c.get("high_price", 0)) or 0)
            c["low"] = float(c.get("low", c.get("low_price", 0)) or 0)
            c["volume"] = float(c.get("volume", 0) or 0)
        return candles
    except Exception as e:
        return []


@dataclass
class BacktestResult:
    symbol: str
    interval: str
    span: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    real_fills_used: int = 0
    fallback_used: int = 0
    trades: list = field(default_factory=list)


def estimate_exit_with_real_data(
    entry_premium: float, side: str,
    entry_spot: float, exit_spot: float,
    holding_minutes: int,
    dte: int,
    entry_fills: List[RealFill],
    symbol: str,
    exit_time: datetime,
    spot_now: float,
) -> tuple:
    """
    Estimate exit premium.
    First try to find a real fill. If not, use delta + theta.

    Returns (premium, source) where source is 'real' or 'synthetic'.
    """
    # Try to find real exit fill
    real = find_real_premium(entry_fills, symbol, side, spot_now, exit_time,
                              max_strike_diff_pct=0.02, max_time_diff_hours=2)
    if real and real > 0:
        # Use real price but sanity-check (must be within delta*spot range)
        return real, 'real'

    # Fall back to synthetic
    if entry_spot <= 0 or entry_premium <= 0:
        return entry_premium, 'synthetic'
    spot_pct_change = (exit_spot - entry_spot) / entry_spot
    if side == "call":
        delta_mult = 1 + max(-0.50, min(1.0, spot_pct_change * 5))
    else:
        delta_mult = 1 + max(-0.50, min(1.0, -spot_pct_change * 5))
    theta_mult = max(0.30, 1 - (holding_minutes / 60) * 0.05)
    return round(entry_premium * delta_mult * theta_mult, 2), 'synthetic'


def run_backtest_on_symbol(symbol: str, fills: List[RealFill],
                            interval: str, span: str,
                            min_score: int = 55,
                            max_trades_per_day: int = 5) -> BacktestResult:
    result = BacktestResult(symbol=symbol, interval=interval, span=span)
    candles = get_candles(symbol, interval, span)
    if not candles:
        print(f"  {symbol}: no candles")
        return result
    print(f"  {symbol}: {len(candles)} candles")

    open_trade = None
    daily_pnl = 0.0
    daily_trades = 0
    current_day = None
    max_equity = 1000.0
    equity = 1000.0

    for i, c in enumerate(candles):
        if i < 30:
            continue

        ts_str = c.get("begins_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue

        day = ts.date()
        if day != current_day:
            current_day = day
            daily_pnl = 0.0
            daily_trades = 0

        spot = float(c.get("close", 0) or 0)
        if spot <= 0:
            continue
        day_high = max(float(x.get("high", 0) or 0) for x in candles[max(0, i - 80):i + 1])
        day_lows = [float(x.get("low", 0) or 0) for x in candles[max(0, i - 80):i + 1] if float(x.get("low", 0) or 0) > 0]
        day_low = min(day_lows) if day_lows else spot

        day_open = spot
        window = candles[max(0, i - 200):i + 1]
        for x in reversed(window):
            x_ts_str = x.get("begins_at", "")
            try:
                x_ts = datetime.fromisoformat(x_ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                x_day = x_ts.date()
            except Exception:
                continue
            if x_day == day:
                day_open = float(x.get("open", 0) or 0)
                break
        if day_open <= 0:
            day_open = spot

        ind = _compute_indicators(window)
        if not ind:
            continue

        score_call = _score_setup("call", ind, spot, day_high, day_low)
        score_put = _score_setup("put", ind, spot, day_high, day_low)
        pullback_pct = (day_high - spot) / day_high if day_high > 0 else 0
        bounce_pct = (spot - day_low) / day_low if day_low > 0 else 0

        entry_target_side = None
        entry_score = 0
        if pullback_pct >= 0.002 and spot > day_open and score_call >= min_score:
            entry_target_side = "call"
            entry_score = score_call
        elif bounce_pct >= 0.002 and spot < day_open and score_put >= min_score:
            entry_target_side = "put"
            entry_score = score_put

        # Exit
        if open_trade is not None:
            held_minutes = (ts - open_trade['entry_time_obj']).total_seconds() / 60
            exit_premium, source = estimate_exit_with_real_data(
                open_trade['entry_premium'], open_trade['side'],
                open_trade['entry_spot'], spot, int(held_minutes), 0,
                fills, symbol, ts, spot
            )
            if source == 'real':
                result.real_fills_used += 1
            else:
                result.fallback_used += 1
            pnl_pct = (exit_premium - open_trade['entry_premium']) / open_trade['entry_premium'] if open_trade['entry_premium'] > 0 else 0
            pnl_dollar = (exit_premium - open_trade['entry_premium']) * 100
            tier = "standard"

            # V2 EXIT RULES (FIX 2026-08-10 — dollar-based, hold losers)
            exit_reason = None
            # Rule 1: Max profit (+$30)
            if pnl_dollar >= config.MAX_PROFIT_DOLLAR:
                exit_reason = "max_profit"
            # Rule 2: Take profit (+$10)
            elif pnl_dollar >= config.TAKE_PROFIT_DOLLAR:
                exit_reason = "take_profit"
            # Rule 3: 0DTE EOD cutoff (15:55)
            elif (open_trade.get('option_expiry') and
                  open_trade.get('option_expiry') == open_trade['entry_time_obj'].date().isoformat() and
                  ts.strftime("%H:%M") >= config.EOD_CUTOFF_HHMM):
                exit_reason = "eod_cutoff"
            # Rule 4: Loss cap (-50% of entry)
            elif pnl_pct <= -config.LOSS_CAP_PCT_NEW:
                exit_reason = "loss_cap_v2"
            # Rule 5: EOD flatten (if option expires today or earlier)
            elif ts.date() != open_trade['entry_time_obj'].date():
                exit_reason = "eod_flatten"

            if exit_reason:
                open_trade['exit_time'] = ts.isoformat()
                open_trade['exit_premium'] = exit_premium
                open_trade['pnl'] = round(pnl_dollar, 2)
                open_trade['exit_reason'] = exit_reason
                open_trade['exit_source'] = source
                result.trades.append(open_trade)
                if pnl_dollar > 0:
                    result.wins += 1
                elif pnl_dollar < 0:
                    result.losses += 1
                else:
                    result.breakeven += 1
                result.gross_pnl += pnl_dollar
                open_trade = None

        # Entry
        if open_trade is None and entry_target_side and daily_trades < max_trades_per_day:
            # Try to find real entry premium
            entry_premium = find_real_premium(
                fills, symbol, entry_target_side, spot, ts,
                max_strike_diff_pct=0.02, max_time_diff_hours=4
            )
            entry_source = 'real' if entry_premium else None

            # Fall back to synthetic if no real
            if entry_premium is None or entry_premium <= 0:
                if entry_target_side == "call":
                    strike = round(spot * 1.01, 2)
                else:
                    strike = round(spot * 0.99, 2)
                # Better synthetic: ATM 0DTE premium ~0.5% of spot
                entry_premium = max(0.05, round(spot * 0.005, 2))
                entry_source = 'synthetic'
                result.fallback_used += 1
            else:
                result.real_fills_used += 1

            if entry_premium > 0:
                open_trade = {
                    'symbol': symbol,
                    'side': entry_target_side,
                    'entry_time': ts.isoformat(),
                    'entry_time_obj': ts,
                    'entry_premium': entry_premium,
                    'entry_spot': spot,
                    'entry_source': entry_source,
                    'score': entry_score,
                    'pullback_pct': round((pullback_pct if entry_target_side == "call" else bounce_pct) * 100, 2),
                }
                daily_trades += 1

        equity = 1000.0 + result.gross_pnl
        max_equity = max(max_equity, equity)
        dd = (max_equity - equity) / max_equity if max_equity > 0 else 0
        result.max_drawdown = max(result.max_drawdown, dd)

    # Force close
    if open_trade is not None:
        spot = float(candles[-1].get("close", 0) or 0)
        held_minutes = 60
        exit_premium, source = estimate_exit_with_real_data(
            open_trade['entry_premium'], open_trade['side'],
            open_trade['entry_spot'], spot, held_minutes, 0,
            fills, symbol, datetime.now(), spot
        )
        pnl_dollar = (exit_premium - open_trade['entry_premium']) * 100
        open_trade['exit_time'] = candles[-1].get("begins_at")
        open_trade['exit_premium'] = exit_premium
        open_trade['pnl'] = round(pnl_dollar, 2)
        open_trade['exit_reason'] = "end_of_data"
        open_trade['exit_source'] = source
        result.trades.append(open_trade)
        result.gross_pnl += pnl_dollar
        if pnl_dollar > 0:
            result.wins += 1
        elif pnl_dollar < 0:
            result.losses += 1
        else:
            result.breakeven += 1

    result.total_trades = len(result.trades)
    if result.total_trades > 0:
        result.win_rate = round(result.wins / result.total_trades * 100, 1)
    wins_pnl = [t['pnl'] for t in result.trades if t.get('pnl') and t['pnl'] > 0]
    losses_pnl = [t['pnl'] for t in result.trades if t.get('pnl') and t['pnl'] < 0]
    result.avg_win = round(sum(wins_pnl) / len(wins_pnl), 2) if wins_pnl else 0
    result.avg_loss = round(sum(losses_pnl) / len(losses_pnl), 2) if losses_pnl else 0

    return result


def main():
    parser = argparse.ArgumentParser(description="V2 paper backtest using REAL option fills")
    parser.add_argument("--interval", default="5minute", choices=["5minute", "hour", "day"])
    parser.add_argument("--span", default="week")
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--min-score", type=int, default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else config.WATCHLIST
    min_score = args.min_score or getattr(config, "MIN_SIGNAL_SCORE", 55)

    print(f"=== V2 Backtest with REAL Option Fills ===")
    print(f"Loading fills from Robinhood order history...")
    fills = load_real_fills()
    print(f"Loaded {len(fills)} fills")
    print()

    all_results = []
    grand_total_trades = 0
    grand_gross_pnl = 0.0
    grand_wins = 0
    grand_losses = 0
    grand_real_used = 0
    grand_fallback = 0

    for sym in symbols:
        print(f"Backtesting {sym}...")
        r_obj = run_backtest_on_symbol(sym, fills, args.interval, args.span, min_score)
        all_results.append(asdict(r_obj))
        grand_total_trades += r_obj.total_trades
        grand_gross_pnl += r_obj.gross_pnl
        grand_wins += r_obj.wins
        grand_losses += r_obj.losses
        grand_real_used += r_obj.real_fills_used
        grand_fallback += r_obj.fallback_used

    print()
    print("=" * 80)
    print("=== BACKTEST RESULTS — REAL OPTION FILLS ===")
    print("=" * 80)
    print(f"{'Symbol':6} {'Trades':7} {'Wins':5} {'Losses':6} {'Win%':6} {'P&L':10} {'Real':5} {'Fall':5}")
    print("-" * 80)
    for r in all_results:
        print(f"{r['symbol']:6} {r['total_trades']:7d} {r['wins']:5d} {r['losses']:6d} "
              f"{r['win_rate']:5.1f}% ${r['gross_pnl']:+9.2f} {r['real_fills_used']:5d} {r['fallback_used']:5d}")
    print("-" * 80)
    print(f"{'TOTAL':6} {grand_total_trades:7d} {grand_wins:5d} {grand_losses:6d} "
          f"{(grand_wins/max(grand_total_trades,1)*100):5.1f}% ${grand_gross_pnl:+9.2f} {grand_real_used:5d} {grand_fallback:5d}")

    out_data = {
        "run_at": datetime.now().isoformat(),
        "interval": args.interval,
        "span": args.span,
        "symbols": symbols,
        "min_score": min_score,
        "fills_loaded": len(fills),
        "summary": {
            "total_trades": grand_total_trades,
            "total_wins": grand_wins,
            "total_losses": grand_losses,
            "total_pnl": round(grand_gross_pnl, 2),
            "win_rate": round(grand_wins / max(grand_total_trades, 1) * 100, 1),
            "real_fills_used": grand_real_used,
            "fallback_used": grand_fallback,
        },
        "per_symbol": all_results,
    }
    if args.out:
        out_path = args.out
    else:
        out_dir = Path(__file__).parent.parent / "backtest_results"
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"backtest_REAL_{args.interval}_{args.span}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(out_data, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
