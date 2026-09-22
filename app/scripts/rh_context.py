"""
Robinhood Context Filters — adds fundamental/news/macro awareness.

Three filters that prevent bad entries:

1. EARNINGS — skip if earnings within X days (binary event risk)
2. NEWS — skip if recent negative headlines
3. VIX — skip calls when VIX > threshold (volatile regime)

All three are cached (TTL varies) so we don't hammer the API.
"""
import os
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"

CACHE_DIR = Path("/tmp/rh_context_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# EARNINGS FILTER
# =============================================================================
EARNINGS_CACHE_FILE = CACHE_DIR / "earnings.json"
EARNINGS_TTL_HOURS = 6
EARNINGS_BLOCK_DAYS = 3  # Skip if earnings within 3 days


def get_earnings_data(force_refresh=False):
    """
    Get upcoming earnings calendar.
    Returns: {symbol: earnings_date_str}
    """
    # Check cache
    if not force_refresh and EARNINGS_CACHE_FILE.exists():
        age_hours = (datetime.now().timestamp() - EARNINGS_CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < EARNINGS_TTL_HOURS:
            with open(EARNINGS_CACHE_FILE) as f:
                return json.load(f)

    # Fetch fresh
    earnings = {}
    try:
        from mcp__robinhood import robinhood_get_earnings_calendar
        cal = robinhood_get_earnings_calendar(range_days=14)
        for entry in cal.get("results", [] or []):
            # Entry format varies; try common shapes
            sym = entry.get("symbol") or entry.get("ticker") or ""
            date_str = entry.get("date") or entry.get("report_date") or entry.get("earnings_date")
            if sym and date_str:
                earnings[sym.upper()] = date_str

        # Cache
        with open(EARNINGS_CACHE_FILE, "w") as f:
            json.dump(earnings, f)
        return earnings
    except Exception as e:
        # Return cached if available
        if EARNINGS_CACHE_FILE.exists():
            with open(EARNINGS_CACHE_FILE) as f:
                return json.load(f)
        return {}


def check_earnings_risk(symbol):
    """
    Check if symbol has earnings within EARNINGS_BLOCK_DAYS.
    Returns: (block, reason)
        block: True/False
        reason: str explanation
    """
    earnings = get_earnings_data()
    sym = symbol.upper()

    if sym not in earnings:
        return False, "no_upcoming_earnings"

    try:
        earnings_date = datetime.fromisoformat(earnings[sym])
    except Exception:
        return False, "date_parse_error"

    days_until = (earnings_date - datetime.now()).days

    if 0 <= days_until <= EARNINGS_BLOCK_DAYS:
        return True, f"earnings_in_{days_until}d"
    return False, f"earnings_in_{days_until}d"


# =============================================================================
# NEWS FILTER
# =============================================================================
NEWS_CACHE_DIR = CACHE_DIR / "news"
NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
NEWS_TTL_HOURS = 1  # Refresh news every hour
NEWS_BLOCK_KEYWORDS = [
    "lawsuit", "investigation", "subpoena", "sec", "fraud",
    "guidance cut", "miss", "downgrade", "warning", "recall",
    "bankruptcy", "delisted", "halt", "investigation",
    "antitrust", "fine", "penalty",
]


def get_recent_news(symbol, force_refresh=False):
    """Get recent news for a symbol (cached)."""
    cache_file = NEWS_CACHE_DIR / f"{symbol.upper()}.json"
    if not force_refresh and cache_file.exists():
        age_hours = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
        if age_hours < NEWS_TTL_HOURS:
            with open(cache_file) as f:
                return json.load(f)

    try:
        from mcp__robinhood import robinhood_get_news
        news = robinhood_get_news(symbol=symbol)
        # Normalize
        items = news.get("results", news if isinstance(news, list) else [])
        headlines = []
        for item in items[:10]:
            title = item.get("title") or item.get("headline") or ""
            published = item.get("published_at") or item.get("datetime") or ""
            if title:
                headlines.append({"title": title, "published": published})

        with open(cache_file, "w") as f:
            json.dump(headlines, f)
        return headlines
    except Exception as e:
        if cache_file.exists():
            with open(cache_file) as f:
                return json.load(f)
        return []


def check_news_sentiment(symbol):
    """
    Check recent news for negative headlines.
    Returns: (block, reason, headlines)
    """
    headlines = get_recent_news(symbol)
    if not headlines:
        return False, "no_news", []

    # Check recent (24h) news for negative keywords
    cutoff = datetime.now() - timedelta(hours=24)
    negative_count = 0
    negative_examples = []

    for item in headlines:
        title_lower = item["title"].lower()
        for kw in NEWS_BLOCK_KEYWORDS:
            if kw in title_lower:
                # Try to filter to recent news
                try:
                    pub_time = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
                    pub_time_naive = pub_time.replace(tzinfo=None)
                    if pub_time_naive > cutoff:
                        negative_count += 1
                        if len(negative_examples) < 3:
                            negative_examples.append(item["title"][:80])
                except Exception:
                    negative_count += 1  # If can't parse, count it
                break

    if negative_count >= 2:
        return True, f"negative_news_{negative_count}", negative_examples
    return False, f"news_ok_{negative_count}", negative_examples


# =============================================================================
# VIX REGIME FILTER
# =============================================================================
VIX_CACHE_FILE = CACHE_DIR / "vix.json"
VIX_TTL_MINUTES = 15  # VIX doesn't change rapidly
VIX_CALL_BLOCK = 25.0  # Don't buy calls when VIX > 25 (panic regime)
VIX_PUT_BLOCK = 12.0   # Don't buy puts when VIX < 12 (complacency)


def get_vix(force_refresh=False):
    """Get current VIX value."""
    if not force_refresh and VIX_CACHE_FILE.exists():
        age_min = (datetime.now().timestamp() - VIX_CACHE_FILE.stat().st_mtime) / 60
        if age_min < VIX_TTL_MINUTES:
            with open(VIX_CACHE_FILE) as f:
                data = json.load(f)
                return data.get("vix")

    try:
        from mcp__robinhood import robinhood_get_index_quotes
        quotes = robinhood_get_index_quotes(symbols=["VIX"])
        # Find VIX value
        vix_value = None
        if isinstance(quotes, list):
            for q in quotes:
                if q.get("symbol") == "VIX":
                    vix_value = float(q.get("last_trade_price") or q.get("last") or 0)
                    break
        elif isinstance(quotes, dict):
            results = quotes.get("results", [])
            for q in results:
                if q.get("symbol") == "VIX":
                    vix_value = float(q.get("last_trade_price") or q.get("last") or 0)
                    break

        if vix_value and vix_value > 0:
            with open(VIX_CACHE_FILE, "w") as f:
                json.dump({"vix": vix_value, "ts": datetime.now().isoformat()}, f)
            return vix_value
    except Exception:
        pass

    # Fallback to cache
    if VIX_CACHE_FILE.exists():
        with open(VIX_CACHE_FILE) as f:
            return json.load(f).get("vix")
    return None


def check_vix_regime(direction):
    """
    Check if current VIX regime permits this direction.

    Args:
        direction: "long_call" or "long_put"

    Returns: (block, reason, vix_value)
    """
    vix = get_vix()
    if vix is None:
        return False, "vix_unknown", None

    if direction == "long_call" and vix > VIX_CALL_BLOCK:
        return True, f"vix_panic_{vix:.1f}", vix
    if direction == "long_put" and vix < VIX_PUT_BLOCK:
        return True, f"vix_complacent_{vix:.1f}", vix
    return False, f"vix_ok_{vix:.1f}", vix


# =============================================================================
# COMBINED CONTEXT CHECK
# =============================================================================
def check_all_context_filters(symbol, direction):
    """
    Run all context filters. Returns (block, reason, details).
    First failing filter wins.

    Args:
        symbol: stock ticker
        direction: "long_call" or "long_put"

    Returns:
        dict with:
            - should_block: bool
            - reasons: list of {filter, blocked, reason, detail}
    """
    details = []

    # 1. Earnings check
    block, reason = check_earnings_risk(symbol)
    details.append({
        "filter": "earnings",
        "blocked": block,
        "reason": reason,
    })

    # 2. News check
    if not block:  # Don't waste API call if already blocked
        block_news, reason_news, headlines = check_news_sentiment(symbol)
        details.append({
            "filter": "news",
            "blocked": block_news,
            "reason": reason_news,
            "headlines": headlines,
        })
        if block_news:
            block = True
            reason = reason_news

    # 3. VIX check
    if not block:
        block_vix, reason_vix, vix_val = check_vix_regime(direction)
        details.append({
            "filter": "vix",
            "blocked": block_vix,
            "reason": reason_vix,
            "vix": vix_val,
        })
        if block_vix:
            block = True
            reason = reason_vix

    return {
        "should_block": block,
        "reasons": details,
        "primary_reason": reason,
    }


if __name__ == "__main__":
    print("=" * 60)
    print("Robinhood Context Filters — Test")
    print("=" * 60)

    for sym in ["NVDA", "TSLA", "AAPL", "MU", "INTC"]:
        print(f"\n{sym}:")
        result = check_all_context_filters(sym, "long_call")
        for d in result["reasons"]:
            status = "❌ BLOCK" if d["blocked"] else "✓ OK"
            print(f"  {status} {d['filter']}: {d['reason']}")
        if result["should_block"]:
            print(f"  >>> BLOCKED: {result['primary_reason']}")
