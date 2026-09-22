"""
Daily Hypothetical Label Generator

Runs after market close. Pulls all ML-rejected signals from today,
fetches price data 5-30 minutes after each signal, computes hypothetical
P&L for each, saves labels to backup drive for future retraining.

This is the "what could have been" training data.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import robin_stocks.robinhood as r

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
BACKUP_LABELS = Path("/mnt/backup-mount/trader-v2/hypothetical-labels")
NAS_LABELS = Path("/mnt/file-cabinet/trader-v2/trade-data/hypothetical-labels")

LOOKAHEAD_MINUTES = [5, 15, 30]
EXPIRY_DAYS_OUT = 3


def fetch_price_lookahead(symbol, signal_time, expiry_date):
    """Fetch stock price at multiple time points after signal."""
    from datetime import datetime, timezone
    et = timezone(timedelta(hours=-4))

    # Get historical 5-min candles for the stock
    try:
        hist = r.stocks.get_stock_historicals(
            symbol,
            interval="5minute",
            span="hour"
        )
    except Exception as e:
        return None

    if not hist:
        return None

    # Find prices at each lookahead point
    prices = {}
    for minutes in LOOKAHEAD_MINUTES:
        target = signal_time + timedelta(minutes=minutes)
        target_ts = target.timestamp()

        # Find closest candle
        closest = None
        closest_diff = float('inf')
        for candle in hist:
            try:
                candle_ts = datetime.fromisoformat(
                    candle['begins_at'].replace('Z', '+00:00')
                ).timestamp()
                diff = abs(candle_ts - target_ts)
                if diff < closest_diff:
                    closest = candle
                    closest_diff = diff
            except Exception:
                continue

        if closest:
            prices[minutes] = float(closest['close_price'])

    return prices


def estimate_option_price(stock_price_now, stock_price_future, current_option_price,
                          option_type, strike, direction):
    """
    Estimate option price change based on underlying stock movement.

    Delta approximation: for ATM options, delta ~0.5 for calls, -0.5 for puts.
    Per $1 stock move, ATM option moves ~$0.50.
    """
    if not current_option_price or current_option_price <= 0:
        return None

    stock_delta = stock_price_future - stock_price_now
    if stock_delta == 0:
        return current_option_price

    # Delta approximation for ATM short-term options
    if direction == 'long_call':
        delta = 0.5 if abs(stock_price_now - strike) < 2 else 0.3
        option_delta = stock_delta * delta
    elif direction == 'long_put':
        delta = -0.5 if abs(stock_price_now - strike) < 2 else -0.3
        option_delta = stock_delta * delta
    else:
        return None

    estimated_price = max(0.01, current_option_price + option_delta)
    return estimated_price


def main():
    today = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")
    print(f"Generating hypothetical labels for {today}")

    BACKUP_LABELS.mkdir(parents=True, exist_ok=True)
    NAS_LABELS.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get all ML-rejected signals today
    signals = conn.execute("""
        SELECT id, timestamp, symbol, direction, components
        FROM signals
        WHERE DATE(timestamp) = ?
          AND rejection_reason LIKE 'ml_skip%'
        ORDER BY timestamp
    """, [today]).fetchall()

    print(f"  Found {len(signals)} ML-rejected signals")

    labels = []
    for sig in signals:
        sig_time = datetime.fromisoformat(sig['timestamp'].replace('Z', '+00:00'))
        sig_time_et = sig_time.astimezone(timezone(timedelta(hours=-4)))

        try:
            components = json.loads(sig['components'] or '{}')
        except Exception:
            components = {}

        # Get current option price from components
        entry_price = components.get('entry_price') or components.get('current_price')
        strike = components.get('option_strike')
        if not entry_price or not strike:
            continue

        # Get stock price now and 5/15/30 min later
        prices = fetch_price_lookahead(sig['symbol'], sig_time_et, None)
        if not prices:
            continue

        stock_now = components.get('stock_price')
        if not stock_now:
            continue

        label = {
            "signal_id": sig['id'],
            "timestamp": sig['timestamp'],
            "symbol": sig['symbol'],
            "direction": sig['direction'],
            "strike": strike,
            "entry_price": entry_price,
            "stock_price_now": stock_now,
            "ml_probability": components.get('ml_probability'),
        }

        # Estimate outcome at each lookahead
        for minutes, stock_future in prices.items():
            estimated_exit = estimate_option_price(
                stock_now, stock_future, entry_price,
                sig['direction'], strike, sig['direction']
            )
            if estimated_exit:
                pnl = estimated_exit - entry_price
                label[f"price_{minutes}min"] = estimated_exit
                label[f"pnl_{minutes}min"] = round(pnl, 2)

        # Determine outcome: did the trade work in the long run?
        # If 30min P&L > 0, it was a win despite ML rejection
        if "pnl_30min" in label:
            label["outcome"] = "win" if label["pnl_30min"] > 0 else "loss"
            label["hypothetical_pnl"] = label["pnl_30min"]

        labels.append(label)

    conn.close()

    # Save to backup drive and NAS
    backup_file = BACKUP_LABELS / f"{today}.json"
    with open(backup_file, 'w') as f:
        json.dump(labels, f, indent=2)

    nas_file = NAS_LABELS / f"{today}.json"
    with open(nas_file, 'w') as f:
        json.dump(labels, f, indent=2)

    # Stats
    wins = sum(1 for l in labels if l.get('outcome') == 'win')
    losses = sum(1 for l in labels if l.get('outcome') == 'loss')
    total_pnl = sum(l.get('hypothetical_pnl', 0) for l in labels)

    print(f"  Generated {len(labels)} labels")
    print(f"  Wins: {wins}, Losses: {losses}")
    print(f"  Total hypothetical P&L: ${total_pnl:+.2f}")
    print(f"  Saved: {backup_file}")
    print(f"  Saved: {nas_file}")


if __name__ == "__main__":
    main()