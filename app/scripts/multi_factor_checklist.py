"""
Multi-Factor Trade Checklist (per article discipline).

Validates each trade with at least 3 confirmations before entry:
1. ✅ Technical analysis (already in V1 signals)
2. 📊 Market trend gate — SPY/QQQ direction must align
3. 📰 News sentiment gate — recent news must be neutral or positive
4. 📅 Earnings gate — block day-of earnings (binary event risk)
5. 📈 Volatility gate — VIX must be reasonable (not panic)

Each gate returns a tuple (passed: bool, reason: str, adjustment: int)
The total adjustment is added to the signal score.

Usage:
    from multi_factor_checklist import validate_trade
    ok, reason, adj = validate_trade(symbol, signal_side, signal_score)
    if ok and signal_score + adj >= MIN_SIGNAL_SCORE:
        # enter trade
"""
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/home/ralph/trader-v2')
sys.path.insert(0, '/home/ralph/trader-v2/scripts')
import config
import robin_stocks.robinhood as r

log = logging.getLogger("v2.checklist")

# State cache (avoid hammering APIs)
_state = {
    "spy_direction": None,
    "spy_checked_at": None,
    "vix_value": None,
    "vix_checked_at": None,
    "news_cache": {},  # symbol -> (news, timestamp)
    "earnings_cache": {},  # symbol -> (date, timestamp)
}

CACHE_TTL_SECONDS = 300  # 5 min cache


def _is_fresh(key: str) -> bool:
    """Check if cache entry is still fresh."""
    check_key = key + "_checked_at"
    if _state.get(check_key) is None:
        return False
    age = (datetime.now() - _state[check_key]).total_seconds()
    return age < CACHE_TTL_SECONDS


def _cache_set(key: str, value, check_key: str):
    """Set a value and its timestamp."""
    _state[key] = value
    _state[check_key] = datetime.now()


# =============================================================================
# GATE 1: Market Trend (SPY/QQQ direction)
# =============================================================================
def check_market_trend() -> tuple[bool, str, int]:
    """
    Confirm SPY/QQQ are not in extreme trends.
    If both are down >1% on the day, block all calls (no recovery setups).
    If both are up >1% on the day, block all calls (no pullbacks in bull grind).
    """
    if _is_fresh("spy_direction"):
        return _state["spy_direction"]

    try:
        spy = r.get_quotes("SPY")
        qqq = r.get_quotes("QQQ")
        if not spy or not qqq:
            return True, "no_market_data", 0

        # FIX 2026-08-05: Compute % change from last_trade_price and previous_close
        # r.get_quotes() does NOT return 'percent_change' field — defaults to 0.0
        # which made the filter always return 'market_neutral' (broken).
        spy_last = float(spy[0].get("last_trade_price", 0) or 0)
        spy_prev = float(spy[0].get("previous_close", 0) or 0)
        qqq_last = float(qqq[0].get("last_trade_price", 0) or 0)
        qqq_prev = float(qqq[0].get("previous_close", 0) or 0)

        if spy_prev > 0:
            spy_chg = (spy_last - spy_prev) / spy_prev * 100
        else:
            spy_chg = float(spy[0].get("percent_change", 0) or 0)
        if qqq_prev > 0:
            qqq_chg = (qqq_last - qqq_prev) / qqq_prev * 100
        else:
            qqq_chg = float(qqq[0].get("percent_change", 0) or 0)

        # Strong downtrend = calls risky (no recovery setups)
        if spy_chg < -1.75 and qqq_chg < -1.75:
            result = (False, f"market_downtrend_SPY{spy_chg:.1f}%_QQQ{qqq_chg:.1f}%", -10)
        # Strong uptrend = calls risky (no pullbacks form in extreme bull grind)
        # FIX 2026-08-04: Raised threshold from 1.0% to 1.75% — was blocking normal bull days
        # where SPY/QQQ +1-2% is healthy and pullbacks still form. Only block extreme moves.
        elif spy_chg > 1.75 and qqq_chg > 1.75:
            result = (False, f"market_uptrend_no_pullbacks_SPY{spy_chg:.1f}%_QQQ{qqq_chg:.1f}%", -10)
        # Normal conditions
        else:
            result = (True, f"market_neutral_SPY{spy_chg:.1f}%_QQQ{qqq_chg:.1f}%", 0)

        _cache_set("spy_direction", result, "spy_checked_at")
        return result
    except Exception as e:
        log.warning(f"Market trend check failed: {e}")
        return True, "check_error", 0


# =============================================================================
# GATE 2: News Sentiment (simple word count)
# =============================================================================
def check_news_sentiment(symbol: str) -> tuple[bool, str, int]:
    """
    Check if recent news is overwhelmingly negative.
    Uses simple word counting on news titles.
    """
    cache_key = f"news_{symbol}"
    if cache_key in _state["news_cache"]:
        cached, ts = _state["news_cache"][cache_key]
        if (datetime.now() - ts).total_seconds() < CACHE_TTL_SECONDS:
            return cached

    try:
        news = r.get_news(symbol)
        if not news:
            result = (True, "no_news", 0)
        else:
            # Simple word count
            positive_words = ['beat', 'surge', 'rally', 'gain', 'high', 'strong', 'up', 'win', 'record', 'growth']
            negative_words = ['miss', 'fall', 'drop', 'crash', 'low', 'weak', 'down', 'loss', 'cut', 'decline']

            positive_count = 0
            negative_count = 0
            for n in news:
                title = (n.get("title") or "").lower()
                for word in positive_words:
                    if word in title:
                        positive_count += 1
                for word in negative_words:
                    if word in title:
                        negative_count += 1

            # If 3x more negative than positive, flag it
            if negative_count > positive_count * 3 and negative_count >= 3:
                result = (False, f"negative_news_{negative_count}neg_{positive_count}pos", -10)
            elif positive_count > negative_count * 2:
                result = (True, f"positive_news_{positive_count}pos_{negative_count}neg", 5)
            else:
                result = (True, f"neutral_news_{positive_count}pos_{negative_count}neg", 0)

        _state["news_cache"][cache_key] = (result, datetime.now())
        return result
    except Exception as e:
        log.warning(f"News check failed for {symbol}: {e}")
        return True, "check_error", 0


# =============================================================================
# GATE 3: Earnings Calendar (block day-of)
# =============================================================================
def check_earnings_calendar(symbol: str) -> tuple[bool, str, int]:
    """
    Block trading if symbol has earnings within 24 hours.
    Earnings cause unpredictable gaps.
    """
    cache_key = f"earnings_{symbol}"
    if cache_key in _state["earnings_cache"]:
        cached, ts = _state["earnings_cache"][cache_key]
        if (datetime.now() - ts).total_seconds() < CACHE_TTL_SECONDS:
            return cached

    try:
        earnings = r.get_earnings(symbol)
        if not earnings:
            result = (True, "no_earnings_data", 0)
        else:
            # Earnings is a list of {date, time, timing, ...}
            # Find the next earnings date
            now = datetime.now()
            for entry in earnings:
                if not isinstance(entry, dict):
                    continue
                earnings_date_str = entry.get("date") or entry.get("report")
                if not earnings_date_str:
                    continue
                try:
                    earnings_date = datetime.fromisoformat(earnings_date_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                days_until = (earnings_date.replace(tzinfo=None) - now).days
                if 0 <= days_until <= 1:
                    result = (False, f"earnings_in_{days_until}d", 0)
                    _state["earnings_cache"][cache_key] = (result, datetime.now())
                    return result

            result = (True, "no_earnings_soon", 0)

        _state["earnings_cache"][cache_key] = (result, datetime.now())
        return result
    except Exception as e:
        log.warning(f"Earnings check failed for {symbol}: {e}")
        return True, "check_error", 0


# =============================================================================
# GATE 4: Volatility Filter (VIX)
# =============================================================================
def check_volatility() -> tuple[bool, str, int]:
    """
    Block trading if VIX is elevated (over 25).
    High VIX = choppy markets, wider spreads, false signals.
    """
    if _is_fresh("vix_value"):
        return _state["vix_value"]

    try:
        # Use Yahoo Finance for VIX (not on Robinhood)
        import yfinance as yf
        vix_data = yf.Ticker("^VIX").history(period="1d")
        if vix_data.empty:
            return True, "no_vix_data", 0
        vix = float(vix_data["Close"].iloc[-1])

        if vix > 30:
            result = (False, f"vix_panic_{vix:.1f}", -20)
        elif vix > 25:
            result = (True, f"vix_high_{vix:.1f}", -5)
        elif vix < 15:
            result = (True, f"vix_low_{vix:.1f}", 5)
        else:
            result = (True, f"vix_normal_{vix:.1f}", 0)

        _cache_set("vix_value", result, "vix_checked_at")
        return result
    except Exception as e:
        log.warning(f"VIX check failed: {e}")
        return True, "check_error", 0


# =============================================================================
# Master checklist function
# =============================================================================
def validate_trade(symbol: str, side: str, signal_score: int, include_calendar: bool = True) -> tuple[bool, str, int, dict]:
    """
    Run all 5 gates. Returns:
      - ok: True if all gates pass
      - reason: summary string
      - adjustment: total score adjustment
      - details: dict of each gate's result
    """
    from economic_calendar import check_economic_events

    gates = {
        "market_trend": check_market_trend(),
        "volatility": check_volatility(),
        "news": check_news_sentiment(symbol),
        "earnings": check_earnings_calendar(symbol),
    }
    if include_calendar:
        gates["economic_calendar"] = check_economic_events()

    # Apply side-specific trend blocking
    if side == "call":
        # Calls need positive/neutral market
        market_ok, market_reason, market_adj = gates["market_trend"]
        if not market_ok:
            return False, f"market_blocks_call: {market_reason}", -100, gates
    elif side == "put":
        # Puts need negative/neutral market
        market_ok, market_reason, market_adj = gates["market_trend"]
        # Note: we could block puts in uptrend, but our market_trend is already asymmetric

    # All gates must pass for entry
    all_pass = all(g[0] for g in gates.values())
    total_adj = sum(g[2] for g in gates.values())

    reasons = [f"{k}: {g[1]}" for k, g in gates.items()]
    return all_pass, " | ".join(reasons), total_adj, gates


if __name__ == "__main__":
    # Test the checklist
    r.login()
    test_symbols = ["MU", "AAPL", "NVDA", "TSLA"]
    for sym in test_symbols:
        for side in ["call", "put"]:
            ok, reason, adj, details = validate_trade(sym, side, 55)
            status = "✅" if ok else "❌"
            print(f"  {status} {sym:5} {side:4} adj={adj:+3d}  {reason[:80]}")
