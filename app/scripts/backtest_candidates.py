"""
Backtest ML-rejected candidates using underlying stock price movement.

Since Robinhood API rate-limits option price queries and doesn't expose
historical option prices per strike, we approximate by:
1. Getting the underlying stock price at signal time
2. Getting the underlying price 15 minutes later
3. Using delta = 0.5 ATM approximation for option P&L
4. For calls: option_pnl = stock_delta_price * 0.5 * 100
5. For puts: option_pnl = -stock_delta_price * 0.5 * 100

This is a rough approximation but gives directional signal.
"""
import json
import sys
import time as time_mod
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, "/home/ralph/trader-v2/scripts")

from robinhood_client import get_client


DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
EXIT_HOLD_MINUTES = 15  # Simulate exiting 15 min after signal
DELTA_ATM = 0.5  # ATM option delta


def get_connection():
    return sqlite3.connect(DB_PATH)


def get_underlying_price_at(client, symbol, target_time_str):
    """Get stock price at or just before target_time_str."""
    try:
        candles = client.get_historicals(symbol, interval="5minute", span="day")
        if not candles:
            return None
        
        target_dt = datetime.fromisoformat(target_time_str.replace('Z', '+00:00'))
        target_ts = int(target_dt.timestamp())
        
        # Find candle at or before target time
        best_candle = None
        for c in candles:
            begins_at = c.get('begins_at', '')
            c_dt = datetime.fromisoformat(begins_at.replace('Z', '+00:00'))
            c_ts = int(c_dt.timestamp())
            if c_ts <= target_ts:
                best_candle = c
            else:
                break
        
        if best_candle:
            return float(best_candle.get('close_price', 0))
        return None
    except Exception as e:
        return None


def get_underlying_price_after_minutes(client, symbol, signal_time_str, minutes):
    """Get stock price approximately N minutes after signal."""
    try:
        candles = client.get_historicals(symbol, interval="5minute", span="day")
        if not candles:
            return None
        
        signal_dt = datetime.fromisoformat(signal_time_str.replace('Z', '+00:00'))
        target_dt = signal_dt + timedelta(minutes=minutes)
        target_ts = int(target_dt.timestamp())
        
        # Find candle at or before target
        best_candle = None
        for c in candles:
            begins_at = c.get('begins_at', '')
            c_dt = datetime.fromisoformat(begins_at.replace('Z', '+00:00'))
            c_ts = int(c_dt.timestamp())
            if c_ts <= target_ts:
                best_candle = c
            else:
                break
        
        if best_candle:
            return float(best_candle.get('close_price', 0))
        return None
    except Exception as e:
        return None


def main():
    client = get_client()
    client._ensure_logged_in()
    
    with open('/tmp/today_candidates.json') as f:
        signals = json.load(f)
    
    print(f"Backtesting {len(signals)} candidate signals...")
    print(f"Using underlying stock price movement × {DELTA_ATM} ATM delta × 100")
    print(f"Hold period: {EXIT_HOLD_MINUTES} minutes\n")
    
    # Cache stock prices by symbol - one fetch per symbol per day
    price_cache = {}
    
    results = []
    wins = 0
    losses = 0
    no_data = 0
    
    for i, sig in enumerate(signals):
        if i % 50 == 0:
            print(f"  Processing {i}/{len(signals)}...")
        
        sym = sig['symbol']
        direction = sig['direction']
        
        if direction not in ('long_call', 'long_put'):
            continue
        
        sig_time = sig['timestamp']
        
        # Get prices (use cache if available)
        if sym not in price_cache:
            entry_price = get_underlying_price_at(client, sym, sig_time)
            exit_price = get_underlying_price_after_minutes(client, sym, sig_time, EXIT_HOLD_MINUTES)
            price_cache[sym] = (entry_price, exit_price)
        else:
            entry_price, exit_price = price_cache[sym]
        
        if entry_price is None or exit_price is None or entry_price <= 0:
            no_data += 1
            continue
        
        # Compute hypothetical option P&L
        # Stock moved by (exit - entry)
        stock_move = exit_price - entry_price
        
        if direction == 'long_call':
            option_pnl = stock_move * DELTA_ATM * 100  # $ per contract
        else:  # long_put
            option_pnl = -stock_move * DELTA_ATM * 100
        
        # Apply ML probability as confidence modifier
        # Extract ML prob from rejection reason if available
        ml_prob = None
        rej_reason = sig.get('rejection_reason')
        if rej_reason and rej_reason.startswith('ml_skip_p'):
            try:
                ml_prob = float(rej_reason.replace('ml_skip_p', ''))
            except:
                pass
        
        label = 1 if option_pnl > 0 else 0
        
        if label == 1:
            wins += 1
        else:
            losses += 1
        
        results.append({
            'signal_id': sig['id'],
            'symbol': sym,
            'direction': direction,
            'signal_time': sig_time,
            'signal_score': sig['score'],
            'ml_prob': ml_prob,
            'rejection_reason': sig.get('rejection_reason'),
            'entry_underlying': entry_price,
            'exit_underlying': exit_price,
            'stock_move': stock_move,
            'option_pnl': round(option_pnl, 2),
            'label': label,
        })
    
    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0
    print(f"\n=== BACKTEST RESULTS ===")
    print(f"Signals processed: {len(results)}")
    print(f"No data: {no_data}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Win rate: {win_rate:.1f}%")
    
    # Break down by score
    by_score = {}
    for r in results:
        score = r['signal_score']
        if score not in by_score:
            by_score[score] = {'wins': 0, 'losses': 0, 'pnl': 0}
        if r['label'] == 1:
            by_score[score]['wins'] += 1
        else:
            by_score[score]['losses'] += 1
        by_score[score]['pnl'] += r['option_pnl']
    
    print("\nBy signal score:")
    for score in sorted(by_score.keys()):
        d = by_score[score]
        total_score = d['wins'] + d['losses']
        wr = d['wins'] / total_score * 100 if total_score > 0 else 0
        print(f"  Score {score:+d}: {d['wins']}W/{d['losses']}L ({wr:.0f}%), P&L ${d['pnl']:.0f}")
    
    # By ML probability bucket
    by_ml = {'<5%': {'wins': 0, 'losses': 0}, '5-10%': {'wins': 0, 'losses': 0}, '>10%': {'wins': 0, 'losses': 0}}
    for r in results:
        p = r.get('ml_prob')
        if p is None:
            continue
        bucket = '<5%' if p < 0.05 else ('5-10%' if p < 0.10 else '>10%')
        if r['label'] == 1:
            by_ml[bucket]['wins'] += 1
        else:
            by_ml[bucket]['losses'] += 1
    
    print("\nBy ML probability (what the bot rejected):")
    for bucket, d in by_ml.items():
        total_ml = d['wins'] + d['losses']
        wr = d['wins'] / total_ml * 100 if total_ml > 0 else 0
        print(f"  ML {bucket}: {d['wins']}W/{d['losses']}L ({wr:.0f}%)")
    
    # Save results
    with open('/tmp/backtest_labels.json', 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} labels to /tmp/backtest_labels.json")


if __name__ == "__main__":
    main()