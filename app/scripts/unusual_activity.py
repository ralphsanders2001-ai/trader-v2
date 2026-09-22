"""
Unusual Options Activity Detector (per article discipline).

Robinhood provides per-option volume and open interest. We can:
- Calculate volume / OI ratio (high = unusual)
- Detect volume spikes vs 5-day baseline
- Surface these in the dashboard

We can't see WHO is trading (block, sweep, etc.) without paid services,
but high volume-to-OI ratio is a strong proxy for unusual activity.

Usage:
    from unusual_activity import scan_unusual_activity
    results = scan_unusual_activity(['MU', 'AAPL', 'NVDA', 'TSLA', 'SPY'])
    for r in results:
        print(f"  {r['symbol']:5} {r['strike']:5} {r['option_type']:4} vol={r['volume']} OI={r['oi']} ratio={r['vol_oi_ratio']:.1f}x")
"""
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, '/home/ralph/trader-v2')
sys.path.insert(0, '/home/ralph/trader-v2/scripts')
import config
import robin_stocks.robinhood as r

log = logging.getLogger("v2.unusual")

# Cache for unusual activity scan
CACHE_PATH = Path("/home/ralph/trader-v2/cache/unusual_activity.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Tunable thresholds
VOL_OI_RATIO_UNUSUAL = 1.0   # volume > 100% of OI = unusual
VOL_OI_RATIO_STRONG = 2.0    # volume > 200% of OI = strong
VOL_OI_RATIO_EXTREME = 5.0   # volume > 500% of OI = extreme


def _get_chain_with_volume(symbol: str) -> list:
    """Get option chain for symbol, including volume and OI."""
    try:
        chain = r.get_options_chain(symbol)
        if not chain:
            return []
        # chain is list of (expiry, strike, type, instrument) tuples
        return chain
    except Exception as e:
        log.warning(f"Failed to get chain for {symbol}: {e}")
        return []


def scan_unusual_activity(symbols: list, top_n: int = 20) -> list:
    """
    Scan symbols for unusual options activity.
    Returns list of dicts with symbol, strike, type, volume, OI, ratio.
    """
    results = []

    for sym in symbols:
        try:
            # Get all available expirations
            chains_data = r.get_chains(sym)
            expirations = chains_data.get("expiration_dates", []) if isinstance(chains_data, dict) else (chains_data or [])
            if not expirations:
                continue

            # Check near-term expirations only (most active)
            near_expiries = expirations[:2] if isinstance(expirations, list) else []

            for expiry in near_expiries:
                try:
                    options = r.find_options_by_expiration(sym, expirationDate=expiry)
                except Exception:
                    continue
                if not options:
                    continue

                for opt in options:
                    try:
                        vol = float(opt.get("volume") or 0)
                        oi = float(opt.get("open_interest") or 0)
                        strike = float(opt.get("strike_price", 0))
                        opt_type = opt.get("type", "")
                        bid = float(opt.get("bid_price", 0) or 0)
                        ask = float(opt.get("ask_price", 0) or 0)
                        last = float(opt.get("last_trade_price", 0) or 0)
                    except (ValueError, TypeError, KeyError):
                        continue

                    # Skip dead contracts
                    if oi < 10 and vol < 10:
                        continue

                    # Calculate ratio
                    if oi > 0:
                        ratio = vol / oi
                    else:
                        ratio = float('inf') if vol > 0 else 0

                    # Classify
                    if ratio >= VOL_OI_RATIO_EXTREME:
                        signal = "EXTREME"
                    elif ratio >= VOL_OI_RATIO_STRONG:
                        signal = "STRONG"
                    elif ratio >= VOL_OI_RATIO_UNUSUAL:
                        signal = "UNUSUAL"
                    else:
                        continue  # skip normal

                    # Calculate dollar value of volume
                    vol_dollar = vol * (last or (bid + ask) / 2 if (bid + ask) else 0)

                    results.append({
                        "symbol": sym,
                        "expiry": expiry,
                        "strike": strike,
                        "option_type": opt_type,
                        "volume": int(vol),
                        "open_interest": int(oi),
                        "vol_oi_ratio": round(ratio, 2),
                        "signal": signal,
                        "last_price": last,
                        "bid": bid,
                        "ask": ask,
                        "volume_dollar_value": round(vol_dollar, 0),
                    })
        except Exception as e:
            log.warning(f"Scan failed for {sym}: {e}")

    # Sort by ratio (most unusual first)
    results.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)
    return results[:top_n]


def save_scan(results: list):
    """Save scan results to cache for dashboard display."""
    try:
        with open(CACHE_PATH, 'w') as f:
            json.dump({
                "results": results,
                "scanned_at": datetime.now().isoformat(),
            }, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save scan: {e}")


def get_cached_results() -> tuple[list, str]:
    """Load cached results."""
    if not CACHE_PATH.exists():
        return [], "never"
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return data.get("results", []), data.get("scanned_at", "unknown")
    except Exception as e:
        return [], f"error: {e}"


if __name__ == "__main__":
    """Test the scanner."""
    r.login()
    syms = ['MU', 'INTC', 'LCID', 'SPCX', 'SMCI', 'AAPL', 'NVDA', 'TSLA']
    results = scan_unusual_activity(syms, top_n=20)
    print(f"\n=== Top {len(results)} unusual options ===")
    print(f"{'Symbol':6} {'Exp':12} {'Strike':>7} {'Type':4} {'Vol':>6} {'OI':>5} {'Ratio':>6} {'Signal':>8} {'$Vol':>7}")
    print("-" * 80)
    for r in results:
        print(f"  {r['symbol']:4} {r['expiry']:12} ${r['strike']:>6.0f} {r['option_type']:4} {r['volume']:>6} {r['open_interest']:>5} {r['vol_oi_ratio']:>5.1f}x {r['signal']:>8} ${r['volume_dollar_value']:>6.0f}")
    save_scan(results)
    print(f"\nSaved to {CACHE_PATH}")
