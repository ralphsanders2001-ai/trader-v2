"""
Technical indicators computed on 5-minute candles.

All functions accept a list of candle dicts with keys:
    'open_price', 'high', 'low', 'close', 'volume', 'begins_at'

Returns plain numeric values for use in scoring.
"""


def closes(candles):
    """Extract close prices as a list."""
    return [float(c.get("close", 0) or c.get("close_price", 0) or 0) for c in candles]


def highs(candles):
    # FIX 2026-08-05: Use high_price fallback (Robinhood returns high_price,
    # not high — without fallback, returns 0 and breaks indicators)
    return [float(c.get("high_price", 0) or c.get("high", 0) or 0) for c in candles]


def lows(candles):
    # FIX 2026-08-05: Use low_price fallback
    return [float(c.get("low_price", 0) or c.get("low", 0) or 0) for c in candles]


def volumes(candles):
    return [float(c.get("volume", 0) or 0) for c in candles]


def sma(values, period):
    """Simple moving average."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values, period):
    """Exponential moving average."""
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    # Seed with SMA
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = (v - ema_val) * multiplier + ema_val
    return ema_val


def rsi(values, period=14):
    """Relative Strength Index."""
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-delta)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values, fast=12, slow=26, signal=9):
    """MACD with histogram. Returns dict with macd_line, signal_line, histogram.

    FIX 2026-08-04: Adaptive period reduction when fewer candles available.
    Standard MACD needs slow+signal=35 candles minimum. For shorter data,
    scale down proportionally (e.g., 12 candles → fast=5, slow=8, signal=3).
    """
    n = len(values)
    if n < fast + 1:
        return None

    # Adaptive: scale periods when we don't have enough candles
    if n < slow + signal:
        # Scale to fit available data, keep ratios similar
        scale = n / (slow + signal + 5)  # leave buffer
        if scale < 0.5:
            # Very few candles - use aggressive scale
            fast_a = max(3, int(fast * scale))
            slow_a = max(fast_a + 1, int(slow * scale))
            signal_a = max(2, int(signal * scale))
        else:
            fast_a = fast
            slow_a = slow
            signal_a = max(2, int(signal * 0.7))  # reduce signal more than fast/slow
    else:
        fast_a = fast
        slow_a = slow
        signal_a = signal

    ema_fast = ema(values, fast_a)
    ema_slow = ema(values, slow_a)
    if ema_fast is None or ema_slow is None:
        return None
    macd_line = ema_fast - ema_slow
    # Compute MACD line series for signal
    macd_series = []
    for i in range(slow_a - 1, len(values)):
        ef = ema(values[:i + 1], fast_a)
        es = ema(values[:i + 1], slow_a)
        if ef is not None and es is not None:
            macd_series.append(ef - es)
    if len(macd_series) < signal_a:
        return {"macd_line": macd_line, "signal_line": macd_line, "histogram": 0}
    sig = ema(macd_series, signal_a)
    return {
        "macd_line": macd_line,
        "signal_line": sig,
        "histogram": macd_line - sig,
    }


def bollinger(values, period=20, stddev=2):
    """Bollinger Bands. Returns upper, middle, lower."""
    if len(values) < period:
        return None
    middle = sma(values, period)
    if middle is None:
        return None
    variance = sum((v - middle) ** 2 for v in values[-period:]) / period
    sd = variance ** 0.5
    return {
        "upper": middle + stddev * sd,
        "middle": middle,
        "lower": middle - stddev * sd,
    }


def atr(candles, period=14):
    """Average True Range."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        # FIX 2026-08-05: Use high_price/low_price fallback
        h = float(candles[i].get("high_price", 0) or candles[i].get("high", 0) or 0)
        l = float(candles[i].get("low_price", 0) or candles[i].get("low", 0) or 0)
        pc = float(candles[i - 1].get("close_price", 0) or candles[i - 1].get("close", 0) or 0)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period




def adx(candles, period=14):
    """
    Average Directional Index (ADX) — measures trend strength.
    Returns dict with adx, plus_di, minus_di.
    ADX > 25 = strong trend
    ADX < 20 = weak/ranging market

    FIX 2026-08-04: Adaptive period reduction when fewer candles available.
    Standard ADX needs 2*period+1=29 candles. For shorter data,
    scale down period to fit available candles.
    """
    n = len(candles)

    # Adaptive: reduce period when not enough candles
    if n < period * 2 + 1:
        # Scale period to fit available data
        # Need at least 2*period for Wilder smoothing + period for ADX smoothing
        if n < period + 1:
            return None
        # New period: n / 3 (need 2*period for DM smoothing + period for ADX)
        new_period = max(2, n // 3)
        if new_period > period:
            new_period = period
    else:
        new_period = period

    if n < new_period * 2 + 1:
        return None

    def get(c, key, prev=False):
        idx = -2 if prev else -1
        i = (idx - 1) if prev else idx
        # candles[i-1] for prev, candles[i] for current
        c_prev = candles[i - 1] if prev else None
        if prev:
            return float(c_prev.get(key) or c_prev.get(key + "_price", 0))
        return float(c.get(key) or c.get(key + "_price", 0))

    # Calculate True Range and Directional Movement
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, len(candles)):
        c = candles[i]
        c_prev = candles[i - 1]
        h = float(c.get("high", 0) or c.get("high_price", 0))
        l = float(c.get("low", 0) or c.get("low_price", 0))
        pc = float(c_prev.get("close", 0) or c_prev.get("close_price", 0))
        ph = float(c_prev.get("high", 0) or c_prev.get("high_price", 0))
        pl = float(c_prev.get("low", 0) or c_prev.get("low_price", 0))

        # True Range
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)

        # Plus/minus DM
        up_move = h - ph
        down_move = pl - l
        if up_move > down_move and up_move > 0:
            plus_dm_list.append(up_move)
        else:
            plus_dm_list.append(0)
        if down_move > up_move and down_move > 0:
            minus_dm_list.append(down_move)
        else:
            minus_dm_list.append(0)

    if len(tr_list) < new_period * 2:
        return None

    # Wilder's smoothing (use new_period, not original period)
    def wilder_smooth(data, p):
        # First value: simple sum of first `p` values
        sm = sum(data[:p])
        result = [sm]
        for v in data[p:]:
            # Wilder's: prev - prev/p + current
            sm = sm - sm / p + v
            result.append(sm)
        return result

    tr_smooth = wilder_smooth(tr_list, new_period)
    plus_dm_smooth = wilder_smooth(plus_dm_list, new_period)
    minus_dm_smooth = wilder_smooth(minus_dm_list, new_period)

    if not tr_smooth:
        return None

    # DI calculations
    plus_di_list = []
    minus_di_list = []
    for pdm, mdm, tr in zip(plus_dm_smooth, minus_dm_smooth, tr_smooth):
        if tr > 0:
            plus_di_list.append((pdm / tr) * 100)
            minus_di_list.append((mdm / tr) * 100)
        else:
            plus_di_list.append(0)
            minus_di_list.append(0)

    # DX list (after first period of DI values)
    dx_list = []
    for pdi, mdi in zip(plus_di_list, minus_di_list):
        if pdi + mdi > 0:
            dx_list.append(abs(pdi - mdi) / (pdi + mdi) * 100)
        else:
            dx_list.append(0)

    if len(dx_list) < new_period:
        return None

    # ADX = Wilder smooth of DX (use new_period)
    adx_sm = sum(dx_list[:new_period]) / new_period
    for v in dx_list[new_period:]:
        adx_sm = (adx_sm * (new_period - 1) + v) / new_period

    return {
        "adx": adx_sm,
        "plus_di": plus_di_list[-1] if plus_di_list else None,
        "minus_di": minus_di_list[-1] if minus_di_list else None,
    }


def trend_strength(adx_value):
    """
    Classify trend strength from ADX value.
    Returns: ('strong_up', 'weak_up', 'ranging', 'weak_down', 'strong_down', 'unknown')
    """
    if adx_value is None:
        return "unknown"
    if adx_value < 20:
        return "ranging"
    elif adx_value < 25:
        return "weak"
    elif adx_value < 50:
        return "strong"
    else:
        return "very_strong"


def vwap(candles):
    """Volume-weighted average price for the candles provided."""
    total_pv = 0
    total_v = 0
    for c in candles:
        # FIX 2026-08-05: Use high_price/low_price fallback
        h = float(c.get("high_price", 0) or c.get("high", 0) or 0)
        l = float(c.get("low_price", 0) or c.get("low", 0) or 0)
        cl = float(c.get("close_price", 0) or c.get("close", 0) or 0)
        typical = (h + l + cl) / 3
        vol = float(c.get("volume", 0) or 0)
        total_pv += typical * vol
        total_v += vol
    if total_v == 0:
        return None
    return total_pv / total_v


def compute_all(candles):
    """Compute a full indicator snapshot for the given candles."""
    if len(candles) < 3:
        return None
    c = closes(candles)
    h = highs(candles)
    l = lows(candles)
    v = volumes(candles)
    return {
        "candles_used": len(candles),
        "last_close": c[-1],
        "ema9": ema(c, 9),
        "ema21": ema(c, 21),
        "sma20": sma(c, 20),
        "rsi14": rsi(c, 14),
        "macd": macd(c),
        "bollinger": bollinger(c),
        "atr14": atr(candles),
        "vwap": vwap(candles),
        "volume_sma20": sma(v, 20),
        "last_volume": v[-1] if v else 0,
        "adx": adx(candles),
            # 2026-08-12: prior-day time-of-day trend (user request)
            "prior_day_tod_trend": prior_day_tod_trend(candles),
    }



def prior_day_tod_trend(candles, window_minutes=15, lookback_days=5):
    """For the current candle's time-of-day, look at the same window on
    the previous N trading days. Returns the average return in the next
    `window_minutes` after that time-of-day on prior days.

    Returns a float (percent): positive = price went up, negative = down.
    Returns None if not enough data.
    """
    if not candles or len(candles) < 10:
        return None

    window_candles = max(1, window_minutes // 5)

    last = candles[-1]
    last_ts = last.get("begins_at") or last.get("session") or ""
    if not last_ts:
        return None

    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        _ET = ZoneInfo("US/Eastern")
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).astimezone(_ET)
    except Exception:
        return None

    current_date = last_dt.date()
    current_hhmm = last_dt.strftime("%H:%M")

    # Group candles by date
    days_seen = {}
    for c in candles:
        ts = c.get("begins_at") or c.get("session") or ""
        if not ts:
            continue
        try:
            ct = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(_ET)
        except Exception:
            continue
        cd = ct.date()
        cl = float(c.get("close_price", 0) or c.get("close", 0) or 0)
        if cd not in days_seen:
            days_seen[cd] = []
        days_seen[cd].append((ct, cl))

    # For each prior day, find candle at/after current_hhmm and next window_candles
    returns = []
    sorted_days = sorted([d for d in days_seen.keys() if d < current_date], reverse=True)
    for d in sorted_days[:lookback_days]:
        c_list = days_seen[d]
        idx = None
        for i, (ct, _) in enumerate(c_list):
            if ct.strftime("%H:%M") >= current_hhmm:
                idx = i
                break
        if idx is None or idx + window_candles >= len(c_list):
            continue
        start_price = c_list[idx][1]
        end_price = c_list[idx + window_candles][1]
        if start_price > 0:
            ret = (end_price - start_price) / start_price
            returns.append(ret)

    if not returns:
        return None
    return sum(returns) / len(returns) * 100


def score(indicators):
    """
    Score a symbol from -100 (strong sell) to +100 (strong buy) using
    the indicators computed above. Used to decide which movers to enter.
    """
    if not indicators:
        return 0, "no_indicators"

    score_val = 0
    components = {}

    # EMA cross (15 points)
    if indicators.get("ema9") and indicators.get("ema21"):
        if indicators["ema9"] > indicators["ema21"]:
            score_val += 15
            components["ema_cross"] = "bullish"
        else:
            score_val -= 15
            components["ema_cross"] = "bearish"

    # RSI (10 points, sweet spot)
    rsi = indicators.get("rsi14")
    if rsi is not None:
        if 30 <= rsi <= 50:
            score_val += 10
            components["rsi"] = "oversold_zone"
        elif 50 < rsi <= 70:
            score_val += 5
            components["rsi"] = "bullish"
        elif rsi > 70:
            # FIX 2026-08-05: Overbought penalty reduced from -20 to -5
            # Reason: Strong bull trends keep RSI >70 for hours; old penalty
            # blocked all calls in trending markets. New logic still flags
            # overbought but doesn't kill the score completely.
            score_val -= 5
            components["rsi"] = "overbought"
        else:
            components["rsi"] = "extreme_oversold"

    # MACD histogram (10 points)
    macd_data = indicators.get("macd")
    if macd_data and macd_data.get("histogram", 0) > 0:
        score_val += 10
        components["macd"] = "bullish_histogram"
    elif macd_data:
        # FIX 2026-08-05: Only penalize bearish MACD if EMA also bearish
        # Reason: Many bull days have negative MACD histogram just before
        # the crossover. Without EMA confirmation, was too strict.
        ema9 = indicators.get("ema9")
        ema21 = indicators.get("ema21")
        if ema9 and ema21 and ema9 > ema21:
            # EMA bullish — let MACD bearish slide
            components["macd"] = "bearish_histogram_ema_overrides"
        else:
            score_val -= 5
            components["macd"] = "bearish_histogram"

    # Volume confirmation (10 points)
    last_vol = indicators.get("last_volume", 0)
    vol_sma = indicators.get("volume_sma20", 0)
    if vol_sma and last_vol > vol_sma * 1.5:
        score_val += 10
        components["volume"] = "breakout"
    elif vol_sma and last_vol > vol_sma * 1.2:
        score_val += 5
        components["volume"] = "elevated"

    # Bollinger position (10 points)
    bb = indicators.get("bollinger")
    close = indicators.get("last_close")
    if bb and close:
        if close <= bb["lower"]:
            score_val += 10
            components["bb"] = "near_lower"
        elif close < bb["upper"] * 0.98:
            score_val += 5
            components["bb"] = "middle_to_upper"
        elif close >= bb["upper"]:
            score_val -= 10
            components["bb"] = "at_upper"

    # VWAP (10 points) — FIX 2026-08-06: symmetric scoring
    vwap_val = indicators.get("vwap")
    if vwap_val and close:
        if close > vwap_val:
            score_val += 10
            components["vwap"] = "above"
        elif close < vwap_val:
            score_val -= 10  # FIX 2026-08-06: was 0 — caused 30-point bull bias
            components["vwap"] = "below"
        else:
            components["vwap"] = "at"

    # Bollinger position (10 points) — FIX 2026-08-06: symmetric
    bb = indicators.get("bollinger")
    close = indicators.get("last_close")
    if bb and close:
        if close <= bb["lower"]:
            score_val += 10
            components["bb"] = "near_lower"
        elif close < bb["upper"] * 0.98:
            score_val += 5
            components["bb"] = "middle_to_upper"
        elif close >= bb["upper"]:
            score_val -= 10  # symmetric (was already -10, kept)
            components["bb"] = "at_upper"
        elif close > bb["middle"]:
            # FIX 2026-08-06: middle area = neutral, no penalty
            components["bb"] = "neutral"
        else:
            # Below middle, above lower
            components["bb"] = "below_middle"

    # RSI (10 points, sweet spot) — FIX 2026-08-06: symmetric
    rsi = indicators.get("rsi14")
    if rsi is not None:
        if 30 <= rsi <= 50:
            score_val += 10
            components["rsi"] = "oversold_zone"
        elif 50 < rsi <= 70:
            score_val += 5
            components["rsi"] = "bullish"
        elif rsi > 70:
            score_val -= 15  # FIX 2026-08-06: was -5 (too weak to trigger puts)
            components["rsi"] = "overbought"
        elif rsi < 30:
            score_val -= 5  # FIX 2026-08-06: bear case for RSI <30
            components["rsi"] = "extreme_oversold"

    # 2026-08-12: Prior-day time-of-day trend (user request)
    # Adds up to 15 points if today's time-of-day has historically been
    # bullish; subtracts up to 15 if historically bearish.
    pdt = indicators.get("prior_day_tod_trend")
    if pdt is not None:
        if pdt > 0:
            # Cap at +15 points for +1% or better typical return
            pts = min(15, int(round(pdt * 15)))
            score_val += pts
            components["prior_day_tod_trend"] = f"bullish_+{pdt:.2f}pct"
        else:
            pts = max(-15, int(round(pdt * 15)))
            score_val += pts
            components["prior_day_tod_trend"] = f"bearish_{pdt:.2f}pct"

    return score_val, components
