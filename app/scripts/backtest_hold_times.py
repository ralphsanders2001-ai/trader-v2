"""
V2 Backtest with VARIABLE hold times.
Tests 15/30/45/60/90/120 min max holds to see which works best.

Constraints:
- 5min candles only available for 1 week (~5 trading days)
- Must close before EOD (4 PM ET) - so trades entered after 4 PM - MAX_HOLD can't run
- Entry time + MAX_HOLD must be <= 4 PM ET same day (or next trading day)

Uses real option fills where available, synthetic otherwise.
"""
import sys
import json
import argparse
from datetime import datetime, date, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, List
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from robinhood_client import get_client
from v1_signals import _compute_indicators, _score_setup


# EOD flatten time (4 PM ET = 20:00 UTC in summer, but let's use ET)
EOD_HOUR = 16  # 4 PM ET
EOD_MINUTE = 0


@dataclass
class HoldTestResult:
    max_hold_minutes: int
    symbol: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    win_rate: float = 0.0
    avg_hold_winners: float = 0.0
    avg_hold_losers: float = 0.0
    trades: list = field(default_factory=list)


def get_candles(symbol: str, interval: str = "5minute", span: str = "week") -> list:
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


def load_real_fills():
    """Load historical fills for premium lookup."""
    import robin_stocks.robinhood as r
    client = get_client()
    client.login()
    orders = r.get_all_option_orders() or []
    fills = []
    for o in orders:
        if o.get('state') != 'filled':
            continue
        legs = o.get('legs', [])
        if not legs:
            continue
        for exec_data in legs[0].get('executions', []):
            try:
                price = float(exec_data.get('price', 0))
                ts_str = exec_data.get('timestamp', '')
                if not ts_str or price <= 0:
                    continue
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
                fills.append({
                    'symbol': o.get('chain_symbol', ''),
                    'strike': float(legs[0].get('strike_price', 0)),
                    'option_type': legs[0].get('option_type', ''),
                    'side': legs[0].get('side', ''),
                    'timestamp': ts,
                    'price': price,
                })
            except Exception:
                pass
    return fills


def find_real_premium(fills, symbol, side, spot, timestamp,
                      max_strike_diff_pct=0.02, max_time_diff_hours=4):
    matches = []
    for f in fills:
        if f['symbol'] != symbol or f['option_type'] != side:
            continue
        if f['strike'] <= 0 or spot <= 0:
            continue
        sd = abs(f['strike'] - spot) / spot
        if sd > max_strike_diff_pct:
            continue
        td = abs((f['timestamp'] - timestamp).total_seconds()) / 3600
        if td > max_time_diff_hours:
            continue
        score = sd * 10 + td
        matches.append((score, f))
    if not matches:
        return None
    matches.sort(key=lambda x: x[0])
    top = matches[:3]
    prices = sorted([m[1]['price'] for m in top])
    if len(prices) >= 2:
        return round(prices[len(prices) // 2], 2)
    return round(prices[0], 2)


def estimate_exit_premium(entry_premium, side, entry_spot, exit_spot,
                          holding_minutes):
    """Simple delta + theta model."""
    if entry_spot <= 0 or entry_premium <= 0:
        return entry_premium
    pct = (exit_spot - entry_spot) / entry_spot
    if side == "call":
        delta_mult = 1 + max(-0.50, min(1.0, pct * 5))
    else:
        delta_mult = 1 + max(-0.50, min(1.0, -pct * 5))
    theta_mult = max(0.30, 1 - (holding_minutes / 60) * 0.05)
    return round(entry_premium * delta_mult * theta_mult, 2)


def eod_for_day(day_date):
    """Get the EOD timestamp for a given day."""
    return datetime.combine(day_date, datetime.min.time()).replace(
        hour=EOD_HOUR, minute=EOD_MINUTE
    )


def run_backtest(symbol: str, fills: list, max_hold_minutes: int,
                 loss_cap_dollar: float = 40, profit_target_pct: float = 0.12,
                 interval: str = "5minute", span: str = "week",
                 min_score: int = 55, max_trades_per_day: int = 5) -> HoldTestResult:
    result = HoldTestResult(max_hold_minutes=max_hold_minutes, symbol=symbol)
    candles = get_candles(symbol, interval, span)
    if not candles:
        return result

    open_trade = None
    daily_trades = 0
    current_day = None

    for i, c in enumerate(candles):
        if i < 30:
            continue

        ts_str = c.get("begins_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            continue

        day = ts.date()
        if day != current_day:
            current_day = day
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
            try:
                x_ts = datetime.fromisoformat(x.get("begins_at", "").replace('Z', '+00:00')).replace(tzinfo=None)
                if x_ts.date() == day:
                    day_open = float(x.get("open", 0) or 0)
                    break
            except Exception:
                continue
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

        # Exit logic
        if open_trade is not None:
            held_minutes = (ts - open_trade['entry_time_obj']).total_seconds() / 60
            exit_premium = estimate_exit_premium(
                open_trade['entry_premium'], open_trade['side'],
                open_trade['entry_spot'], spot, held_minutes
            )
            # Check real fill if available
            real = find_real_premium(fills, symbol, open_trade['side'], spot, ts)
            if real:
                exit_premium = real

            pnl_pct = (exit_premium - open_trade['entry_premium']) / open_trade['entry_premium'] if open_trade['entry_premium'] > 0 else 0
            pnl_dollar = (exit_premium - open_trade['entry_premium']) * 100

            eod = eod_for_day(day)
            # Force exit at EOD regardless of hold time
            time_to_eod = (eod - ts).total_seconds() / 60

            exit_reason = None
            if pnl_dollar <= -loss_cap_dollar:
                exit_reason = "loss_cap"
            elif pnl_pct >= profit_target_pct:
                exit_reason = "profit_target"
            elif held_minutes >= max_hold_minutes:
                exit_reason = f"max_hold_{max_hold_minutes}m"
            elif time_to_eod <= 0:
                exit_reason = "eod_flatten"

            if exit_reason:
                open_trade['exit_time'] = ts.isoformat()
                open_trade['exit_premium'] = exit_premium
                open_trade['pnl'] = round(pnl_dollar, 2)
                open_trade['exit_reason'] = exit_reason
                open_trade['held_minutes'] = held_minutes
                result.trades.append(open_trade)
                if pnl_dollar > 0:
                    result.wins += 1
                elif pnl_dollar < 0:
                    result.losses += 1
                result.gross_pnl += pnl_dollar
                open_trade = None

        # Entry
        if open_trade is None and entry_target_side and daily_trades < max_trades_per_day:
            entry_premium = find_real_premium(fills, symbol, entry_target_side, spot, ts)
            if not entry_premium or entry_premium <= 0:
                entry_premium = max(0.05, round(spot * 0.005, 2))
            if entry_premium > 0:
                open_trade = {
                    'symbol': symbol,
                    'side': entry_target_side,
                    'entry_time': ts.isoformat(),
                    'entry_time_obj': ts,
                    'entry_premium': entry_premium,
                    'entry_spot': spot,
                    'score': entry_score,
                }
                daily_trades += 1

    # Force close at EOD or end of data
    if open_trade is not None:
        spot = float(candles[-1].get("close", 0) or 0)
        held_minutes = (datetime.fromisoformat(candles[-1].get("begins_at", "").replace('Z', '+00:00')).replace(tzinfo=None) - open_trade['entry_time_obj']).total_seconds() / 60
        exit_premium = estimate_exit_premium(
            open_trade['entry_premium'], open_trade['side'],
            open_trade['entry_spot'], spot, held_minutes
        )
        pnl_dollar = (exit_premium - open_trade['entry_premium']) * 100
        open_trade['exit_time'] = candles[-1].get("begins_at")
        open_trade['exit_premium'] = exit_premium
        open_trade['pnl'] = round(pnl_dollar, 2)
        open_trade['exit_reason'] = "end_of_data"
        open_trade['held_minutes'] = held_minutes
        result.trades.append(open_trade)
        result.gross_pnl += pnl_dollar
        if pnl_dollar > 0:
            result.wins += 1
        elif pnl_dollar < 0:
            result.losses += 1

    result.total_trades = len(result.trades)
    if result.total_trades > 0:
        result.win_rate = round(result.wins / result.total_trades * 100, 1)
    win_holds = [t['held_minutes'] for t in result.trades if t.get('pnl', 0) > 0 and t.get('held_minutes') is not None]
    loss_holds = [t['held_minutes'] for t in result.trades if t.get('pnl', 0) < 0 and t.get('held_minutes') is not None]
    result.avg_hold_winners = round(sum(win_holds) / len(win_holds), 1) if win_holds else 0
    result.avg_hold_losers = round(sum(loss_holds) / len(loss_holds), 1) if loss_holds else 0

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="MU,INTC,LCID,SPCX,SMCI")
    parser.add_argument("--holds", default="15,30,45,60,90,120")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    symbols = args.symbols.split(",")
    holds = [int(h) for h in args.holds.split(",")]

    print("=== V2 Hold Time Backtest (with REAL fills) ===")
    print(f"Symbols: {symbols}")
    print(f"Hold times to test: {holds} minutes")
    print(f"Loading real fills...")

    fills = load_real_fills()
    print(f"Loaded {len(fills)} fills")
    print()

    # Run for each hold time
    all_results = []
    grand_totals = {}  # hold -> (trades, wins, pnl)

    for max_hold in holds:
        print(f"=== MAX HOLD = {max_hold} minutes ===")
        total_trades = 0
        total_wins = 0
        total_pnl = 0.0
        win_holds_all = []
        loss_holds_all = []

        for sym in symbols:
            r = run_backtest(sym, fills, max_hold)
            total_trades += r.total_trades
            total_wins += r.wins
            total_pnl += r.gross_pnl
            win_holds_all.extend([t['held_minutes'] for t in r.trades if t.get('pnl', 0) > 0])
            loss_holds_all.extend([t['held_minutes'] for t in r.trades if t.get('pnl', 0) < 0])
            win_rate = round(r.wins / max(r.total_trades, 1) * 100, 1)
            print(f"  {sym:5}: {r.total_trades} trades, {r.wins} wins ({win_rate}%), P&L ${r.gross_pnl:+.2f}, "
                  f"avg win hold {r.avg_hold_winners:.1f}min, avg loss hold {r.avg_hold_losers:.1f}min")

        win_rate = round(total_wins / max(total_trades, 1) * 100, 1)
        avg_win_hold = round(sum(win_holds_all) / max(len(win_holds_all), 1), 1) if win_holds_all else 0
        avg_loss_hold = round(sum(loss_holds_all) / max(len(loss_holds_all), 1), 1) if loss_holds_all else 0
        print(f"  TOTAL: {total_trades} trades, {total_wins} wins ({win_rate}%), P&L ${total_pnl:+.2f}")
        print(f"         avg hold: winners {avg_win_hold:.1f}min vs losers {avg_loss_hold:.1f}min")
        print()

        grand_totals[max_hold] = {
            'trades': total_trades,
            'wins': total_wins,
            'pnl': total_pnl,
            'win_rate': win_rate,
            'avg_win_hold': avg_win_hold,
            'avg_loss_hold': avg_loss_hold,
        }
        all_results.append({
            'max_hold_minutes': max_hold,
            'totals': grand_totals[max_hold],
        })

    # Final comparison
    print("=" * 80)
    print("=== HOLD TIME COMPARISON ===")
    print("=" * 80)
    print(f"{'Max Hold':12} {'Trades':8} {'Wins':6} {'Win%':7} {'P&L':12} {'WinHold':10} {'LossHold':10}")
    print("-" * 80)
    for max_hold, t in grand_totals.items():
        print(f"  {max_hold:5} min  {t['trades']:8} {t['wins']:6} {t['win_rate']:6.1f}% "
              f"${t['pnl']:+10.2f} {t['avg_win_hold']:8.1f}min {t['avg_loss_hold']:8.1f}min")

    if args.out:
        out_path = args.out
    else:
        out_dir = Path(__file__).parent.parent / "backtest_results"
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"backtest_HOLD_{ts}.json"

    out_data = {
        'run_at': datetime.now().isoformat(),
        'symbols': symbols,
        'hold_times_tested': holds,
        'fills_loaded': len(fills),
        'results': all_results,
        'totals_by_hold': {str(k): v for k, v in grand_totals.items()},
    }
    with open(out_path, 'w') as f:
        json.dump(out_data, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
