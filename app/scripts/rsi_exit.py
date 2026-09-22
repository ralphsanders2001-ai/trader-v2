"""
RSI-Based Exit Strategy

Uses 5-minute candle data to compute RSI(14) on the underlying stock and
the option's intrinsic behavior, then applies smart exits:

For LONG_CALL positions:
- Exit if RSI > 75 (overbought) AND in profit (>= 8%)
- Exit if RSI > 80 (very overbought) regardless of P&L

For LONG_PUT positions:
- Exit if RSI < 25 (oversold) AND in profit (>= 8%)
- Exit if RSI < 20 (very oversold) regardless of P&L

Additionally, tracks RSI divergence:
- If price making new highs but RSI is making lower highs = bearish divergence
- If price making new lows but RSI making higher lows = bullish divergence
- Divergence + position in profit = exit signal

This runs at every poll (5-15s) and is FAST (microseconds for the rule
check, ~50-100ms to fetch candles + compute RSI).
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def compute_rsi(prices, period=14):
    """
    Compute RSI (Relative Strength Index) using Wilder's smoothing.

    Args:
        prices: list of closing prices (oldest first)
        period: RSI period (default 14)

    Returns:
        float: current RSI value (0-100)
        None if not enough data
    """
    if len(prices) < period + 1:
        return None

    prices = np.array(prices, dtype=float)
    deltas = np.diff(prices)

    # First average gain/loss (simple average)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Wilder's smoothing for remaining periods
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0  # all gains, very overbought

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi)


def get_cached_candles(symbol, ttl_seconds=60):
    """
    Get 5-minute candles with caching to avoid hammering Robinhood.

    Returns list of (timestamp, close_price) tuples, oldest first.
    """
    import json

    cache_path = Path(f"/tmp/rsi_cache_{symbol}.json")
    now = datetime.now()

    # Try cache first
    if cache_path.exists():
        cache_age = now.timestamp() - cache_path.stat().st_mtime
        if cache_age < ttl_seconds:
            with open(cache_path) as f:
                return json.load(f)

    # Fetch fresh
    try:
        from robinhood_client import get_client

        client = get_client()
        candles = client.get_historicals(symbol, interval="5minute", span="week")

        if not candles:
            return None

        # Format: dict with keys begins_at, close_price, etc.
        result = []
        for c in candles:
            ts_str = c.get("begins_at", "")
            close = float(c.get("close_price", 0) or 0)
            if close > 0:
                result.append([ts_str, close])

        # Cache
        with open(cache_path, "w") as f:
            json.dump(result, f)

        return result
    except Exception as e:
        # Log and return cached if available
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return None


def compute_rsi_divergence(prices, lookback=20):
    """
    Detect RSI divergence:
    - Bearish: price making higher high, RSI making lower high
    - Bullish: price making lower low, RSI making higher low

    Returns:
        "bearish_div" | "bullish_div" | None
    """
    if len(prices) < lookback + 14:
        return None

    rsi_series = []
    for i in range(14, len(prices) + 1):
        r = compute_rsi(prices[:i], period=14)
        if r is not None:
            rsi_series.append(r)

    if len(rsi_series) < lookback:
        return None

    # Find recent swing points in price
    recent_prices = prices[-lookback:]
    recent_rsi = rsi_series[-lookback:]

    # Recent high vs earlier high
    midpoint = lookback // 2
    price_high_recent = max(recent_prices[midpoint:])
    price_high_earlier = max(recent_prices[:midpoint])
    rsi_high_recent = max(recent_rsi[midpoint:])
    rsi_high_earlier = max(recent_rsi[:midpoint])

    # Bearish divergence: price higher, RSI lower
    if price_high_recent > price_high_earlier and rsi_high_recent < rsi_high_earlier:
        return "bearish_div"

    # Bullish divergence: price lower, RSI higher
    price_low_recent = min(recent_prices[midpoint:])
    price_low_earlier = min(recent_prices[:midpoint])
    rsi_low_recent = min(recent_rsi[midpoint:])
    rsi_low_earlier = min(recent_rsi[:midpoint])

    if price_low_recent < price_low_earlier and rsi_low_recent > rsi_low_earlier:
        return "bullish_div"

    return None


def check_rsi_exit(position):
    """
    Check if a position should be exited based on RSI signals.

    Args:
        position: dict with keys:
            - symbol
            - option_type (call/put)
            - entry_price
            - current_price (bid)
            - quantity

    Returns:
        dict with:
            - should_exit: bool
            - reason: str
            - rsi: float
            - divergence: str or None
            - detail: str
        or None if no signal (don't exit)
    """
    symbol = position["symbol"]
    option_type = position.get("option_type", "call")
    entry = position["entry_price"]
    current = position.get("current_price", position.get("last_poll_price", 0))

    if current <= 0 or entry <= 0:
        return None

    pnl_pct = (current - entry) / entry

    # Get candles (cached 60s)
    candles = get_cached_candles(symbol, ttl_seconds=60)
    if not candles or len(candles) < 20:
        return None

    closes = [c[1] for c in candles]

    # Compute current RSI
    rsi = compute_rsi(closes, period=14)
    if rsi is None:
        return None

    # Check divergence
    divergence = compute_rsi_divergence(closes, lookback=20)

    result = {
        "should_exit": False,
        "reason": None,
        "rsi": rsi,
        "divergence": divergence,
        "detail": "",
    }

    # === Long call exits ===
    if option_type == "call":
        # Very overbought — exit regardless of P&L
        if rsi > 80:
            result["should_exit"] = True
            result["reason"] = "rsi_extreme_overbought"
            result["detail"] = f"RSI={rsi:.1f} > 80 (very overbought)"
            return result

        # Overbought + in profit — exit to lock in gains
        if rsi > 75 and pnl_pct >= 0.08:
            result["should_exit"] = True
            result["reason"] = "rsi_overbought_profit"
            result["detail"] = f"RSI={rsi:.1f} > 75 with +{pnl_pct*100:.1f}% profit"
            return result

        # Bearish divergence + in profit — exit
        if divergence == "bearish_div" and pnl_pct >= 0.05:
            result["should_exit"] = True
            result["reason"] = "bearish_divergence_profit"
            result["detail"] = f"Bearish divergence detected with +{pnl_pct*100:.1f}% profit"
            return result

    # === Long put exits ===
    elif option_type == "put":
        # Very oversold — exit regardless of P&L
        if rsi < 20:
            result["should_exit"] = True
            result["reason"] = "rsi_extreme_oversold"
            result["detail"] = f"RSI={rsi:.1f} < 20 (very oversold)"
            return result

        # Oversold + in profit
        if rsi < 25 and pnl_pct >= 0.08:
            result["should_exit"] = True
            result["reason"] = "rsi_oversold_profit"
            result["detail"] = f"RSI={rsi:.1f} < 25 with +{pnl_pct*100:.1f}% profit"
            return result

        # Bullish divergence + in profit
        if divergence == "bullish_div" and pnl_pct >= 0.05:
            result["should_exit"] = True
            result["reason"] = "bullish_divergence_profit"
            result["detail"] = f"Bullish divergence detected with +{pnl_pct*100:.1f}% profit"
            return result

    return result  # Return info even if no exit signal (for logging)


def get_rsi_status(symbol):
    """Get current RSI for a symbol (used for dashboard)."""
    candles = get_cached_candles(symbol, ttl_seconds=60)
    if not candles or len(candles) < 20:
        return {"rsi": None, "signal": "no_data", "divergence": None}

    closes = [c[1] for c in candles]
    rsi = compute_rsi(closes, period=14)
    divergence = compute_rsi_divergence(closes, lookback=20)

    if rsi is None:
        return {"rsi": None, "signal": "no_data", "divergence": None}

    # Determine signal
    if rsi > 80:
        signal = "extreme_overbought"
    elif rsi > 70:
        signal = "overbought"
    elif rsi < 20:
        signal = "extreme_oversold"
    elif rsi < 30:
        signal = "oversold"
    else:
        signal = "neutral"

    return {
        "rsi": round(rsi, 1),
        "signal": signal,
        "divergence": divergence,
    }


if __name__ == "__main__":
    print("=" * 60)
    print("RSI Exit Strategy — Test")
    print("=" * 60)

    # Test with synthetic data
    # Simulate an overbought scenario: price went up consistently
    test_prices = [100 + i * 0.5 for i in range(30)]  # Strong uptrend

    rsi = compute_rsi(test_prices, period=14)
    print(f"\nStrong uptrend RSI: {rsi:.1f}")

    # Simulate a sideways market
    test_prices = [100 + (i % 3 - 1) * 0.2 for i in range(30)]
    rsi = compute_rsi(test_prices, period=14)
    print(f"Sideways RSI: {rsi:.1f}")

    # Simulate a downtrend
    test_prices = [110 - i * 0.5 for i in range(30)]
    rsi = compute_rsi(test_prices, period=14)
    print(f"Strong downtrend RSI: {rsi:.1f}")

    print("\nNote: Real RSI uses Robinhood 5-minute candle data.")
