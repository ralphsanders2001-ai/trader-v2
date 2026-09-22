"""Market regime classifier.

Classifies the underlying (SPY/QQQ/NVDA) into one of 5 regimes at signal time.
Uses simple, robust statistics — no neural nets, no heavy ML libraries.

Regimes:
0 = range_low (low vol, no trend)
1 = range_high (high vol, no trend)
2 = trend_up (positive slope, normal vol)
3 = trend_down (negative slope, normal vol)
4 = volatile (extreme vol, no clear direction)

Inputs: a list of 5-minute candles (oldest first).
Output: dict with regime_id, vol_zscore, trend_slope, regime_label.

The classifier lives next to signals so the v2 retrain can use it as a feature.
"""
from __future__ import annotations
import math
from typing import Iterable, Sequence


def _closes(candles: Iterable[dict]) -> list[float]:
    # FIX 2026-08-13: Robinhood returns close_price, not close.
    # Accept either key so tests work.
    out = []
    for c in candles:
        if 'close' in c:
            out.append(float(c['close']))
        elif 'close_price' in c:
            out.append(float(c['close_price']))
    return out


def _log_returns(candles: Sequence[dict]) -> list[float]:
    closes = _closes(candles)
    if len(closes) < 2:
        return []
    out = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0:
            out.append(math.log(cur / prev))
    return out


def _realized_vol(returns: Sequence[float], window: int = 30) -> float:
    """Std-dev of log returns over the last `window` bars."""
    if len(returns) < 2:
        return 0.0
    sample = returns[-window:]
    mean = sum(sample) / len(sample)
    var = sum((r - mean) ** 2 for r in sample) / max(1, len(sample) - 1)
    return math.sqrt(var)


def _trend_slope(candles: Sequence[dict], window: int = 60) -> float:
    """Linear regression slope of close over the last `window` bars.

    Returns slope in %/bar. Positive = uptrend, negative = downtrend.
    """
    closes = _closes(candles)
    if len(closes) < 2:
        return 0.0
    sample = closes[-window:]
    n = len(sample)
    if n < 2:
        return 0.0
    sum_x = sum(range(n))
    sum_y = sum(sample)
    sum_xy = sum(i * sample[i] for i in range(n))
    sum_xx = sum(i * i for i in range(n))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    # Normalize to %/bar relative to mean price
    mean_price = sum_y / n
    if mean_price <= 0:
        return 0.0
    return 100.0 * slope / mean_price


def _vol_zscore(returns: Sequence[float], lookback: int = 60) -> float:
    """Z-score of current realized vol vs trailing mean."""
    if len(returns) < lookback:
        return 0.0
    # Vol-of-vol: compare the last 10-bar vol to the trailing 60-bar history
    recent = returns[-10:]
    history = returns[:-10]
    if len(history) < 5:
        return 0.0
    cur_vol = _realized_vol(recent, window=len(recent))
    # Sample historical vol in 10-bar windows
    hist_vols = []
    for i in range(0, len(history) - 9, 10):
        chunk = history[i:i + 10]
        hist_vols.append(_realized_vol(chunk, window=len(chunk)))
    if len(hist_vols) < 3:
        return 0.0
    mean_v = sum(hist_vols) / len(hist_vols)
    var_v = sum((v - mean_v) ** 2 for v in hist_vols) / max(1, len(hist_vols) - 1)
    std_v = math.sqrt(var_v)
    if std_v == 0:
        return 0.0
    return (cur_vol - mean_v) / std_v


# Thresholds (in %/bar) — calibrated for 5-min bars on liquid ETFs
# 0.05 %/bar ≈ 0.6 %/hour ≈ mild trend
# 0.10 %/bar ≈ 1.2 %/hour ≈ strong trend
SLOPE_TREND_THRESHOLD = 0.05  # %/bar
SLOPE_STRONG_THRESHOLD = 0.10  # %/bar
VOL_ZSCORE_HIGH = 1.0
VOL_ZSCORE_EXTREME = 2.0

# Vol thresholds relative to recent baseline
# Realized vol of SPY 5-min bars is usually ~0.05-0.15 % in normal times
HIGH_VOL_THRESHOLD = 0.20  # % per bar


def classify(candles: Sequence[dict]) -> dict:
    """Classify the current market regime.

    Args:
        candles: list of dicts with 'close' key, oldest first.

    Returns:
        dict with keys: regime_id (int 0-4), vol_zscore (float),
        trend_slope_pct (float), realized_vol_pct (float), regime_label (str).
    """
    if not candles or len(candles) < 5:
        return {
            'regime_id': 0,
            'regime_label': 'range_low',
            'vol_zscore': 0.0,
            'trend_slope_pct': 0.0,
            'realized_vol_pct': 0.0,
        }

    returns = _log_returns(candles)
    vol = _realized_vol(returns) * 100  # convert to %
    slope = _trend_slope(candles)
    vol_z = _vol_zscore(returns)

    # Decision tree
    if vol_z > VOL_ZSCORE_EXTREME or vol > HIGH_VOL_THRESHOLD * 2:
        regime_id = 4
        label = 'volatile'
    elif vol_z > VOL_ZSCORE_HIGH or vol > HIGH_VOL_THRESHOLD:
        if slope > SLOPE_STRONG_THRESHOLD:
            regime_id = 2
            label = 'trend_up'
        elif slope < -SLOPE_STRONG_THRESHOLD:
            regime_id = 3
            label = 'trend_down'
        else:
            regime_id = 1
            label = 'range_high'
    else:
        if slope > SLOPE_TREND_THRESHOLD:
            regime_id = 2
            label = 'trend_up'
        elif slope < -SLOPE_TREND_THRESHOLD:
            regime_id = 3
            label = 'trend_down'
        else:
            regime_id = 0
            label = 'range_low'

    return {
        'regime_id': regime_id,
        'regime_label': label,
        'vol_zscore': float(vol_z),
        'trend_slope_pct': float(slope),
        'realized_vol_pct': float(vol),
    }


if __name__ == '__main__':
    # Smoke test
    fake = [{'close': 100 + i * 0.05} for i in range(120)]
    print(classify(fake))
    fake_volatile = [{'close': 100 + (i % 10) * 0.5} for i in range(120)]
    print(classify(fake_volatile))