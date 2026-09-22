"""
V1-style signal generator: momentum_pullback_signal.

FIX 2026-08-04: Robust checklist import.
The previous try/except at module load made _checklist_validate=None on
import failure, but then call site produced NameError.
Instead: import multi_factor_checklist and use its function directly.

Adopted from /home/ralph/robinhood-trainer/signals.py (the working V1).
Uses Robinhood 5-min candles, EMA20/50/200 trend, RSI, Bollinger, Volume surge.

Scoring (0-100):
  - Base: 50
  - +15 if all 3 EMAs in bullish alignment (call) / all bearish (put)
  - +10 for golden cross (EMA50 greater than EMA200)
  - +10 for RSI in healthy range (40-65 for calls, greater than 60 for puts)
  - +10 for BB position (lower for calls, upper for puts)
  - +5  for volume surge (over 1.5x avg)
  - -15 if extended near resistance (call) or support (put)

Entry gate:
  - Pullback from day high greater than or equal 0.2%
  - Spot greater than day open (bullish bar)
  - Score greater than or equal 55

This produces fewer but higher-conviction signals than V2's broken formula.
"""
import multi_factor_checklist as _multi_factor_checklist_module


def _checklist_validate(symbol, side, score):
    """Call validate_trade, returning (ok, reason, adj, details)."""
    return _multi_factor_checklist_module.validate_trade(symbol, side, score)


from dataclasses import dataclass, asdict
from typing import Optional
import logging

import config
from robinhood_client import get_client

log = logging.getLogger("v2.v1signals")


@dataclass
class V1Signal:
    symbol: str
    side: str                # "call" or "put"
    strike: float
    expiry: str
    option_premium: float
    action: str              # "buy" or "skip"
    reason: str
    score: int = 0
    pullback_pct: float = 0.0
    spot: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    day_open: float = 0.0
    rsi: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    golden_cross: Optional[bool] = None
    bb_position: Optional[str] = None
    volume_ratio: Optional[float] = None
    premium_target: Optional[float] = None
    premium_stop: Optional[float] = None
    underlying_target: Optional[float] = None
    underlying_stop: Optional[float] = None


def _ema(closes, period):
    """Standard EMA (Wilder-style smoothing)."""
    if len(closes) < period:
        return None
    seed = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    val = seed
    for c in closes[period:]:
        val = (c - val) * multiplier + val
    return val


def _rsi(closes, period=14):
    """RSI on close prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains.append(d)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-d)
    if not any(losses):
        return 100.0
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def _compute_indicators(candles):
    """Pull indicator snapshot from candles list.

    Threshold lowered from 30 to 12 candles (FIX 2026-08-04):
    - MACD needs slow(26)+signal(9)=35 candles (with EMA helpers now adapted for fewer candles)
    - ADX needs 2*period+1=29 candles (with shorter smoothing for early values)
    - With 12 candles, EMA20 works, EMA50+ need daily fallback, EMA200 needs daily fallback
    """
    if not candles or len(candles) < 12:
        return None
    closes = [float(c.get("close", 0) or c.get("close_price", 0) or 0) for c in candles]
    volumes = [float(c.get("volume", 0) or 0) for c in candles]

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50) if len(closes) >= 50 else None
    ema200 = _ema(closes, 200) if len(closes) >= 200 else None
    rsi = _rsi(closes, 14)

    # Bollinger (20-period, 2 std)
    if len(closes) >= 20:
        window = closes[-20:]
        mean = sum(window) / 20
        var = sum((c - mean) ** 2 for c in window) / 20
        sd = var ** 0.5
        bb_upper = mean + 2 * sd
        bb_lower = mean - 2 * sd
        last = closes[-1]
        if last >= bb_upper:
            bb_pos = "upper"
        elif last <= bb_lower:
            bb_pos = "lower"
        else:
            bb_pos = "middle"
    else:
        bb_pos = None

    # Volume surge
    vol_ratio = None
    if len(volumes) >= 20 and sum(volumes[-20:]) > 0:
        avg_vol = sum(volumes[-20:]) / 20
        if avg_vol > 0:
            vol_ratio = volumes[-1] / avg_vol

    # MACD (per article: trend confirmation)
    from indicators import macd as _macd
    macd_data = _macd(closes, fast=12, slow=26, signal=9)

    # ADX (per article: trend strength)
    from indicators import adx as _adx, trend_strength as _trend_strength
    adx_data = _adx(candles, period=14)

    return {
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "bb_position": bb_pos,
        "volume_ratio": vol_ratio,
        "golden_cross": ema50 > ema200 if (ema50 and ema200) else None,
        "macd": macd_data,
        "adx": adx_data.get("adx") if adx_data else None,
        "plus_di": adx_data.get("plus_di") if adx_data else None,
        "minus_di": adx_data.get("minus_di") if adx_data else None,
        "trend_strength": _trend_strength(adx_data.get("adx")) if adx_data else "unknown",
    }


def _fetch_daily_emas(symbol: str):
    """
    Fetch daily candles and compute EMA50/200 fallback values.

    FIX 2026-08-04: Intraday 5-min candles don't accumulate enough
    to compute EMA50 (50) or EMA200 (200) within a trading day.
    Use daily candles as fallback to always have trend context.

    Returns dict with ema50, ema200, golden_cross or None on error.
    """
    try:
        import robin_stocks.robinhood as r
        # Get 200+ daily candles (we only need closes)
        daily = r.get_stock_historicals(symbol, interval="day", span="year")
        if not daily or len(daily) < 50:
            return None
        # Sort by date ascending (Robinhood returns descending)
        daily_sorted = sorted(daily, key=lambda c: c.get("begins_at", ""))
        closes = [float(c.get("close_price", 0) or c.get("close", 0) or 0) for c in daily_sorted]
        ema50_d = _ema(closes, 50) if len(closes) >= 50 else None
        ema200_d = _ema(closes, 200) if len(closes) >= 200 else None
        golden_d = (ema50_d > ema200_d) if (ema50_d and ema200_d) else None
        return {"ema50": ema50_d, "ema200": ema200_d, "golden_cross": golden_d}
    except Exception as e:
        log.debug(f"_fetch_daily_emas error for {symbol}: {e}")
        return None


def _score_setup(side: str, ind: dict, spot: float, day_high: float, day_low: float) -> int:
    """
    V1's _score_setup, ported. Returns score 0-100.
    """
    score = config.SIGNAL_BASE_SCORE
    ema20 = ind.get("ema20")
    ema50 = ind.get("ema50")
    ema200 = ind.get("ema200")
    golden = ind.get("golden_cross")
    bb_pos = ind.get("bb_position")
    rsi = ind.get("rsi")
    vol_ratio = ind.get("volume_ratio")

    if side == "call":
        if ema20 and ema50 and ema200:
            if spot > ema20 and spot > ema50 and spot > ema200:
                score += config.SCORE_EMA_TREND_ALL_ABOVE
        if golden:
            score += config.SCORE_GOLDEN_CROSS
        if bb_pos == "lower":
            score += config.SCORE_BB_POSITION_BONUS
        if rsi is not None and 40 < rsi < 65:
            score += config.SCORE_RSI_BONUS
    elif side == "put":
        if ema20 and spot < ema20:
            score += config.SCORE_EMA20_BELOW_PUT
        if ema50 and spot < ema50:
            score += config.SCORE_EMA50_BELOW_PUT
        if bb_pos == "upper":
            score += config.SCORE_BB_POSITION_BONUS
        if rsi is not None and rsi > 60:
            score += config.SCORE_PUT_RSI

    if vol_ratio is not None and vol_ratio > config.VOLUME_SURGE_THRESHOLD:
        score += config.SCORE_VOLUME_SURGE_BONUS

    # === ARTICLE-ALIGNED: TREND CONFIRMATION (MACD + ADX) ===
    adx_val = ind.get("adx")
    plus_di = ind.get("plus_di")
    minus_di = ind.get("minus_di")
    macd_data = ind.get("macd")

    # ADX trend strength gate
    if adx_val is not None and adx_val >= 25:
        # Strong trend — add bonus
        score += 10
        if adx_val >= 40:
            score += 5  # very strong
    elif adx_val is not None and adx_val < 20:
        # Ranging market — penalize (no edge)
        score -= 5

    # MACD histogram confirms direction
    if macd_data and macd_data.get("histogram") is not None:
        histogram = macd_data["histogram"]
        if side == "call" and histogram > 0:
            score += 8  # bullish momentum
        elif side == "put" and histogram < 0:
            score += 8  # bearish momentum

    # DI direction alignment
    if plus_di is not None and minus_di is not None:
        if side == "call" and plus_di > minus_di:
            score += 5  # bullish direction
        elif side == "put" and minus_di > plus_di:
            score += 5  # bearish direction

    # Penalty for being extended
    if side == "call" and day_high > 0 and spot > 0.995 * day_high:
        score -= config.SCORE_RESISTANCE_PENALTY
    if side == "put" and day_low > 0 and spot < 1.005 * day_low:
        score -= config.SCORE_RESISTANCE_PENALTY

    return max(0, min(100, score))


def _find_pullback_option(symbol: str, side: str, spot: float,
                          max_otm_pct: float = 0.03,
                          max_spread_pct: float = 0.30,
                          min_oi: int = 50):
    """
    Find a near-ATM option contract matching V1's contract-quality criteria.
    Returns dict with strike, expiry, premium, otm_pct or None.
    """
    client = get_client()
    try:
        # Get option chain
        import robin_stocks.robinhood as r
        chains = r.get_chains(symbol) or {}
        expiries = chains.get("expiration_dates", []) if isinstance(chains, dict) else []
        if not expiries:
            return None
        # Find closest expiry that's still 0DTE or 1DTE (V1 prefers same-day)
        from datetime import datetime, date
        today = datetime.now().date()
        valid_expiries = []
        for exp in expiries[:7]:  # first week of expiries
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if 0 <= dte <= 7:
                    valid_expiries.append((dte, exp))
            except Exception:
                continue
        if not valid_expiries:
            return None
        valid_expiries.sort(key=lambda x: x[0])
        chosen_expiry = valid_expiries[0][1]

        # Find strikes
        option_type = "call" if side == "call" else "put"
        options_list = r.find_options_by_expiration(
            symbol, expirationDate=chosen_expiry, optionType=option_type, info=None
        ) or []
        if not options_list:
            return None

        # Pick strike closest to ATM
        best = None
        best_diff = float("inf")
        for opt in options_list:
            try:
                strike = float(opt.get("strike_price", 0))
                diff = abs(strike - spot)
                if diff < best_diff:
                    best_diff = diff
                    best = opt
            except Exception:
                continue
        if not best:
            return None

        strike = float(best.get("strike_price", 0))
        otm_pct = (strike - spot) / spot if side == "call" else (spot - strike) / spot
        if otm_pct > max_otm_pct:
            return None

        # Get market data for premium + spread check
        market = client.get_option_market_data(symbol, chosen_expiry, strike, option_type)
        if not market or market.get("bid", 0) <= 0 or market.get("ask", 0) <= 0:
            return None

        bid = market["bid"]
        ask = market["ask"]
        spread_pct = (ask - bid) / ((bid + ask) / 2) if (bid + ask) > 0 else 999
        if spread_pct > max_spread_pct:
            return None

        oi = int(float(best.get("open_interest", 0) or 0))
        if oi < min_oi:
            return None

        premium = ask if ask > 0 else bid
        if premium <= 0:
            return None

        return {
            "strike": strike,
            "expiry": chosen_expiry,
            "premium": round(premium, 2),
            "otm_pct": round(otm_pct * 100, 2),
            "spread_pct": round(spread_pct * 100, 2),
            "open_interest": oi,
            "instrument_id": best.get("id", ""),
        }
    except Exception as e:
        log.debug(f"_find_pullback_option error for {symbol}: {e}")
        return None


def momentum_pullback_signal(symbol: str, side: str = "call",
                             min_score: int = None,
                             pullback_threshold: float = None,
                             max_otm_pct: float = None,
                             max_spread_pct: float = None,
                             min_oi: int = None) -> V1Signal:
    """
    V1's momentum_pullback_signal. Returns V1Signal with action="buy" if entry
    criteria met, else action="skip".
    """
    min_score = min_score if min_score is not None else getattr(config, "MIN_SIGNAL_SCORE", 55)
    pullback_threshold = pullback_threshold if pullback_threshold is not None else getattr(config, "PULLBACK_THRESHOLD", 0.002)
    max_otm_pct = max_otm_pct if max_otm_pct is not None else getattr(config, "MAX_OTM_PCT", 0.03)
    max_spread_pct = max_spread_pct if max_spread_pct is not None else getattr(config, "MAX_BID_ASK_SPREAD_PCT", 0.30)
    min_oi = min_oi if min_oi is not None else getattr(config, "MIN_OPEN_INTEREST", 50)

    client = get_client()
    try:
        quote = client.get_stock_quote(symbol)
    except Exception as e:
        return V1Signal(symbol, side, 0.0, "", 0.0, "skip", f"quote error: {e}")
    if not quote:
        return V1Signal(symbol, side, 0.0, "", 0.0, "skip", "no quote")
    spot = quote.get("last", 0)
    if spot <= 0:
        return V1Signal(symbol, side, 0.0, "", 0.0, "skip", "no price")

    # Get intraday candles (5min, today)
    try:
        candles = client.get_historicals(symbol, interval="5minute", span="day")
    except Exception as e:
        return V1Signal(symbol, side, 0.0, "", 0.0, "skip", f"history error: {e}")
    if not candles or len(candles) < 12:
        return V1Signal(symbol, side, 0.0, "", 0.0, "skip", "not enough candles")

    # Day high/low/open
    highs = [float(c.get("high", 0) or c.get("high_price", 0) or 0) for c in candles]
    lows = [float(c.get("low", 0) or c.get("low_price", 0) or 0) for c in candles]
    opens = [float(c.get("open", 0) or c.get("open_price", 0) or 0) for c in candles]
    day_high = max(highs) if highs else spot
    day_low = min(lows) if lows else spot
    day_open = opens[0] if opens else spot

    # Compute indicators
    ind = _compute_indicators(candles)
    if not ind:
        return V1Signal(symbol, side, 0.0, "", 0.0, "skip", "indicator failure")

    # FIX 2026-08-04: Fall back to daily candles for EMA50/200 when intraday has too few candles
    if ind.get("ema50") is None or ind.get("ema200") is None:
        daily_emas = _fetch_daily_emas(symbol)
        if daily_emas:
            if ind.get("ema50") is None and daily_emas.get("ema50"):
                ind["ema50"] = daily_emas["ema50"]
            if ind.get("ema200") is None and daily_emas.get("ema200"):
                ind["ema200"] = daily_emas["ema200"]
            # Recompute golden cross if needed
            if ind.get("golden_cross") is None and ind.get("ema50") and ind.get("ema200"):
                ind["golden_cross"] = ind["ema50"] > ind["ema200"]

    # Score
    score = _score_setup(side, ind, spot, day_high, day_low)

    # Pullback entry rule
    pullback = (day_high - spot) / day_high if day_high > 0 else 0
    bounce = (spot - day_low) / day_low if day_low > 0 else 0

    # For puts, the logic is inverted: looking for bounce from low
    if side == "call":
        # Pattern 1: Morning dip — pullback after open, spot still above open
        morning_dip = pullback >= pullback_threshold and spot > day_open and score >= min_score
        # Pattern 2 (FIX 2026-08-05): Sell-the-open — pullback from day high
        # when spot has faded below open. Requires larger pullback (0.5% vs 0.2%)
        # and stricter score (min_score + 5) to filter weak setups.
        sell_the_open = (
            pullback >= 0.005  # 0.5% pullback (more conservative)
            and spot < day_open  # price below open (selling pressure)
            and pullback_threshold < 0.5  # active pullback regime
            and score >= min_score + 5  # higher bar for counter-trend
        )
        entry_met = morning_dip or sell_the_open
    else:
        # puts: spot below open, bounced from low, score for bearish setup
        entry_met = bounce >= pullback_threshold and spot < day_open and score >= min_score

    if not entry_met:
        return V1Signal(
            symbol=symbol, side=side, strike=0.0, expiry="", option_premium=0.0,
            action="skip",
            reason=f"score {score}: pullback {pullback*100:.2f}%, spot {spot:.2f}, open {day_open:.2f}",
            score=score, pullback_pct=round(pullback * 100, 2), spot=spot,
            day_high=day_high, day_low=day_low, day_open=day_open,
            rsi=ind.get("rsi"), ema20=ind.get("ema20"), ema50=ind.get("ema50"),
            ema200=ind.get("ema200"), golden_cross=ind.get("golden_cross"),
            bb_position=ind.get("bb_position"), volume_ratio=ind.get("volume_ratio"),
        )

    # Multi-factor checklist validation (per article discipline)
    try:
        ck_ok, ck_reason, ck_adj, ck_details = _checklist_validate(symbol, side, score)
        if not ck_ok:
            return V1Signal(
                symbol=symbol, side=side, strike=0.0, expiry="", option_premium=0.0,
                action="skip",
                reason=f"checklist_block: {ck_reason[:200]}",
                score=score, pullback_pct=round(pullback * 100, 2), spot=spot,
                day_high=day_high, day_low=day_low, day_open=day_open,
                rsi=ind.get("rsi"), ema20=ind.get("ema20"), ema50=ind.get("ema50"),
                ema200=ind.get("ema200"), golden_cross=ind.get("golden_cross"),
                bb_position=ind.get("bb_position"), volume_ratio=ind.get("volume_ratio"),
            )
        score += ck_adj  # apply checklist adjustment
    except Exception as e:
        log.warning(f"Checklist evaluation failed for {symbol}: {e}")

    # Find the option contract
    option = _find_pullback_option(symbol, side, spot, max_otm_pct, max_spread_pct, min_oi)
    if not option:
        return V1Signal(
            symbol=symbol, side=side, strike=0.0, expiry="", option_premium=0.0,
            action="skip", reason=f"score {score}: no qualifying contract (OTM/spread/OI)",
            score=score, pullback_pct=round(pullback * 100, 2), spot=spot,
            day_high=day_high, day_low=day_low, day_open=day_open,
        )

    premium_target_mult = getattr(config, "PREMIUM_TARGET_MULT", 1.20)
    premium_stop_mult = getattr(config, "PREMIUM_STOP_MULT", 0.70)
    underlying_target_mult = getattr(config, "UNDERLYING_TARGET_MULT", 1.008)
    underlying_stop_mult = getattr(config, "UNDERLYING_STOP_MULT", 0.993)

    return V1Signal(
        symbol=symbol, side=side,
        strike=option["strike"], expiry=option["expiry"],
        option_premium=option["premium"], action="buy",
        reason=f"score {score}: pullback {pullback*100:.2f}%, rsi {ind.get('rsi')}, bb {ind.get('bb_position')}",
        score=score, pullback_pct=round(pullback * 100, 2), spot=spot,
        day_high=day_high, day_low=day_low, day_open=day_open,
        rsi=ind.get("rsi"), ema20=ind.get("ema20"), ema50=ind.get("ema50"),
        ema200=ind.get("ema200"), golden_cross=ind.get("golden_cross"),
        bb_position=ind.get("bb_position"), volume_ratio=ind.get("volume_ratio"),
        premium_target=round(option["premium"] * premium_target_mult, 2),
        premium_stop=round(option["premium"] * premium_stop_mult, 2),
        underlying_target=round(spot * underlying_target_mult, 2),
        underlying_stop=round(spot * underlying_stop_mult, 2),
    )


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    sig = momentum_pullback_signal(symbol)
    print(sig)
