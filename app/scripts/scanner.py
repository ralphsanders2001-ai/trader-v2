"""
Scanner module — finds top movers and generates entry signals.

Flow:
1. Fetch top N movers (configurable)
2. Merge with user watchlist override (always include those)
3. Skip symbols in cooldown or blocked for day
4. Skip symbols with open positions
5. Fetch 5m candles for each candidate
6. Compute indicators + score
7. Log signal to DB (audit)
8. Return top-scored candidates for entry
"""
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/ralph/trader-v2")
sys.path.insert(0, "/home/ralph/trader-v2/scripts")

import config
import config_loader
from database import get_connection, log_signal
from robinhood_client import get_client
import indicators


ET = ZoneInfo("US/Eastern")


def get_top_movers():
    """Fetch top N movers from Robinhood. Returns list of symbol strings.
    2026-09-18: RH locked out — return empty list; get_candidate_symbols()
    already falls back to the configured watchlist, which now covers 27
    high-liquidity symbols (broader than the movers endpoint anyway)."""
    return []


def is_symbol_enabled(symbol):
    """Per-symbol trading switch (dashboard checkboxes). A symbol listed in
    config.SYMBOLS_DISABLED cannot be traded (signals rejected) but stays on
    the watchlist so re-enabling needs no other config change."""
    return symbol not in set(getattr(config, "SYMBOLS_DISABLED", []) or [])


def get_candidate_symbols():
    """Merge standard watchlist + user overrides. Symbols disabled on the
    settings page are still listed (so the UI shows them) but executor
    rejects their signals — see is_symbol_enabled."""
    watchlist = list(config.WATCHLIST or [])
    user_picks = list(config.USER_WATCHLIST_OVERRIDE or [])
    forced = list(config.FORCED_TURBULENT_SYMBOLS or [])

    # Combine, dedupe, preserve order
    seen = set()
    result = []
    for sym in watchlist + user_picks + forced:
        sym = sym.upper().strip()
        if sym and sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result


def is_symbol_blocked(symbol):
    """Check if symbol is in cooldown or blocked for today."""
    conn = get_connection()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM symbol_daily_state WHERE symbol=? AND trading_day=?",
        (symbol, today)
    ).fetchone()
    conn.close()
    if not row:
        return False, "no_state"
    if row["blocked_for_day"]:
        return True, "blocked_for_day"
    if row["cooldown_until"]:
        try:
            until = datetime.fromisoformat(row["cooldown_until"])
            if datetime.now(ET) < until:
                return True, f"cooldown_until_{row['cooldown_until']}"
        except Exception:
            pass
    return False, "ok"


def is_circuit_breaker_active():
    """Check if today's circuit breaker has tripped."""
    conn = get_connection()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM circuit_breaker WHERE trading_day=?",
        (today,)
    ).fetchone()
    conn.close()
    if row and row["tripped"]:
        return True, row["reason"]
    return False, None


def has_open_position(symbol):
    """Check if symbol has an open position in DB."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE symbol=?",
        (symbol,),
    ).fetchone()
    conn.close()
    return row["cnt"] > 0


def has_open_position_for_other_mode(symbol, current_mode):
    """FIX 2026-08-10: Two-bot coordination.

    When paper and live bots run in parallel, neither should open a position
    in a symbol the OTHER mode already has open. Prevents double-buying the
    same contract.

    Args:
        symbol: ticker like "NVDA"
        current_mode: "paper" or "live" — which bot is asking

    Returns: True if the other mode has an open position in this symbol.
    """
    other_mode = "live" if current_mode == "paper" else "paper"
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE symbol=? AND source=?",
        (symbol, f"system_{other_mode}"),
    ).fetchone()
    conn.close()
    return row["cnt"] > 0


# Cache for optionability check — avoids hitting API for every scan.
# True results are trusted for the whole trading day. False results are only
# cached for _OPTIONABLE_RETRY_MIN (rate-limit soft-blocks return empty lists,
# and a day-long False lockout silently stops all trading for that symbol).
_OPTIONABLE_CACHE = {}
_OPTIONABLE_CACHE_DATE = None
_OPTIONABLE_NEG_TTL = 30 * 60  # seconds a False result stays cached


def is_symbol_optionable(symbol):
    """Check if a symbol has tradeable options on Robinhood.

    Caches True results for the trading day, False results for
    _OPTIONABLE_NEG_TTL minutes. Returns True if the symbol has at least one
    tradeable option contract on the next monthly expiration. False otherwise.
    """
    global _OPTIONABLE_CACHE, _OPTIONABLE_CACHE_DATE
    now = datetime.now(ET).timestamp()
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # Reset cache daily
    if _OPTIONABLE_CACHE_DATE != today:
        _OPTIONABLE_CACHE = {}
        _OPTIONABLE_CACHE_DATE = today

    if symbol in _OPTIONABLE_CACHE:
        cached, at = _OPTIONABLE_CACHE[symbol]
        if cached or (now - at) < _OPTIONABLE_NEG_TTL:
            return cached

    try:
        # 2026-09-18: RH locked out — use Alpaca options snapshots instead
        # FIX 2026-09-21: the old check targeted only the next MONTHLY (Friday
        # 7+ days out). The Alpaca feed carries near-dated weeklies only, so
        # every megacap (SPY/QQQ/NVDA/AMD/MU/...) returned 0 contracts at the
        # monthly and was dropped all day with "no_options" while weeklies
        # existed. Now: accept ANY expiry inside the 2-7 DTE window — the same
        # window executor.find_option_for_signal() actually buys from.
        import alpaca_options as ao
        from datetime import timedelta
        today = datetime.now(ET).date()
        valid_expiries = []
        for i in range(2, 8):  # DTE 2..7 inclusive
            d = today + timedelta(days=i)
            valid_expiries.append(d.strftime("%Y-%m-%d"))
        result = False
        for _exp in valid_expiries:
            opts = ao.find_options_by_expiration(symbol, expirationDate=_exp, optionType="call")
            if opts and len(opts) > 0:
                result = True
                break
    except Exception:
        # On error, fail-open (assume optionable) to avoid silently dropping
        # valid symbols due to API hiccups
        result = True

    _OPTIONABLE_CACHE[symbol] = (result, now)
    return result


def generate_signal(symbol):
    """
    Generate an entry signal for one symbol using 5m candles.
    Returns dict with score, components, direction, candles_used, or None
    if rejected.
    """
    # Reject during no-trade window
    if config.ENABLE_NO_TRADE_WINDOW:
        now_et = datetime.now(ET).time()
        start = datetime.strptime(config.NO_TRADE_START_HHMM, "%H:%M").time()
        end = datetime.strptime(config.NO_TRADE_END_HHMM, "%H:%M").time()
        if start <= now_et < end:
            return None

    # Check min candles for signal
    # Use span="week" so we get yesterday's candles too — this lets the
    # scanner fire signals earlier in the morning when today's market just
    # opened and there are only a few 5m candles available.
    # Without this, EMA/SMA/RSI/MACD/BBands all return None because they
    # need 30+ data points, and we wait until ~12:30 PM for first signals.
    client = get_client()
    candles = client.get_historicals(symbol, interval="5minute", span="week")
    if not candles or len(candles) < config.MIN_CANDLES_FOR_SIGNAL:
        return None

    # Restrict to last N candles (configurable, default 50)
    candles = candles[-config.PRIMARY_CANDLE_COUNT:]
    ind = indicators.compute_all(candles)
    if not ind:
        return None

    score_val, components = indicators.score(ind)

    # Determine direction. ADAPTIVE THRESHOLD (FIX 2026-08-05):
    # Static ±50 threshold is too rigid — strong bull/bear days rarely
    # generate signals because indicators get overbought/oversold.
    # Instead, scale threshold based on market regime:
    #   - Trending strong (RSI extreme): raise threshold to ±60 (require more conviction)
    #   - Normal range (RSI 40-60): use base ±50
    #   - Choppy/ranging (low ADX proxy): lower threshold to ±35 (catch early signals)
    direction = None
    base_threshold = config.MIN_SCORE_THRESHOLD  # 50

    # Detect regime from indicators
    rsi_val = ind.get("rsi14", 50)
    if rsi_val is None:
        rsi_val = 50

    # FIX 2026-08-05: Lower base thresholds — adaptive logic was still
    # too strict. Now base thresholds are 35/40/45 (was 50/55/60).
    # Reason: With 50-60 score ceiling, signals were too rare.
    if rsi_val > 75 or rsi_val < 25:
        # Extreme overbought/oversold — still require conviction
        adaptive_threshold = 45
    elif 40 <= rsi_val <= 60:
        # Normal range — base threshold
        adaptive_threshold = 40
    else:
        # 25-40 or 60-75 — moderate pullback zone
        adaptive_threshold = 35

    # Bull market bonus — if all 3 EMAs are bullish AND VWAP is above,
    # the symbol is in confirmed uptrend. Lower the bar by 5.
    ema9 = ind.get("ema9")
    ema21 = ind.get("ema21")
    vwap_val = ind.get("vwap")
    last_close = ind.get("last_close")
    if ema9 and ema21 and vwap_val and last_close:
        if ema9 > ema21 and last_close > vwap_val:
            # Confirmed uptrend — make it easier to enter
            adaptive_threshold -= 5  # 30/35/40

    # FIX 2026-08-06: Bear-market mirror — when EMA9<EMA21 AND price<VWAP,
    # confirmed downtrend, LOWER threshold for long_put entries by 5 (symmetric).
    # This prevents the asymmetry that caused 5 long_call losses today when market
    # was flat-to-down (no recovery setups formed).
    if ema9 and ema21 and vwap_val and last_close:
        if ema9 < ema21 and last_close < vwap_val:
            # Confirmed downtrend — make it easier to enter puts
            # Use a per-direction threshold: long_put threshold drops, long_call threshold rises
            score_val_for_direction = score_val
            # Boost negative score to make long_put signal easier
            if score_val <= 0:
                score_val_for_direction = score_val - 5  # more negative = easier put signal
            else:
                # In a downtrend, suppress longs entirely
                score_val_for_direction = score_val - 10  # harder to trigger long_call
        else:
            score_val_for_direction = score_val
    else:
        score_val_for_direction = score_val

    # 2026-09-15 (Ralph): MIN_SCORE_THRESHOLD is now a live settings-page knob.
    # It acts as a FLOOR on the adaptive threshold below — the effective gate
    # is max(adaptive_threshold, MIN_SCORE_THRESHOLD). Set it low (e.g. 30) to
    # let the adaptive RSI/trend logic rule alone; set it higher to demand
    # conviction in every regime.
    _score_floor = getattr(config, "MIN_SCORE_THRESHOLD", 0)
    if _score_floor > adaptive_threshold:
        adaptive_threshold = _score_floor

    if score_val_for_direction >= adaptive_threshold:
        direction = "long_call"
    elif score_val_for_direction <= -adaptive_threshold:
        direction = "long_put"

    # 2026-08-11: Honor ALLOWED_OPTION_TYPES filter (user wants calls only)
    try:
        import config as _cfg
        allowed = getattr(_cfg, "ALLOWED_OPTION_TYPES", ["call", "put"])
    except Exception:
        allowed = ["call", "put"]
    if direction == "long_put" and "put" not in allowed:
        direction = "none"
    if direction == "long_call" and "call" not in allowed:
        direction = "none"

    return {
        "symbol": symbol,
        "score": score_val,
        "components": components,
        "direction": direction,
        "candles_used": len(candles),
        "candles_snapshot": candles[-5:],
        "indicators": ind,
        "adaptive_threshold": adaptive_threshold,
        "regime": "overbought" if rsi_val > 70 else "oversold" if rsi_val < 30 else "normal",
    }


# log_signal was moved to database.py


def scan():
    """Run a single scan cycle. Returns list of trade candidates."""
    # 1. Check circuit breaker
    tripped, reason = is_circuit_breaker_active()
    if tripped:
        print(f"[SCAN] Circuit breaker active: {reason}")
        return []

    # 2. Get candidate symbols
    candidates = get_candidate_symbols()
    print(f"[SCAN] {len(candidates)} candidates from top movers + overrides")

    # 3. Filter out blocked/cooldown symbols
    eligible = []
    for sym in candidates:
        blocked, why = is_symbol_blocked(sym)
        if blocked:
            log_signal({
                "symbol": sym, "score": 0, "components": {},
                "direction": None, "candles_used": 0,
            }, status="rejected", rejection_reason=why)
            continue
        if has_open_position(sym):
            log_signal({
                "symbol": sym, "score": 0, "components": {},
                "direction": None, "candles_used": 0,
            }, status="rejected", rejection_reason="open_position")
            continue
        # FIX 2026-08-10: Two-bot coordination — skip if the OTHER mode
        # (paper vs live) has an open position in this symbol. Prevents
        # both bots from buying the same contract.
        current_mode = getattr(config, "MODE", "paper").lower()
        if has_open_position_for_other_mode(sym, current_mode):
            log_signal({
                "symbol": sym, "score": 0, "components": {},
                "direction": None, "candles_used": 0,
            }, status="rejected", rejection_reason=f"other_mode_held")
            continue
        # FIX 2026-08-10: Skip symbols that don't have tradeable options
        # — saves API calls + avoids wasted ML/LLM work on tickers where
        # order placement will fail anyway.
        if not is_symbol_optionable(sym):
            log_signal({
                "symbol": sym, "score": 0, "components": {},
                "direction": None, "candles_used": 0,
            }, status="rejected", rejection_reason="no_options")
            continue
        eligible.append(sym)

    print(f"[SCAN] {len(eligible)} eligible after filters")

    # 4. Generate signals
    signals = []
    for sym in eligible:
        sig = generate_signal(sym)
        if sig and sig["direction"]:
            log_signal(sig, status="candidate")
            signals.append(sig)
            print(f"  {sym}: score={sig['score']:+d} dir={sig['direction']}")
        else:
            log_signal({
                "symbol": sym, "score": 0, "components": {},
                "direction": None, "candles_used": 0,
            }, status="rejected", rejection_reason="no_signal")

    # 5. Sort by absolute score (strongest signals first)
    signals.sort(key=lambda s: abs(s["score"]), reverse=True)

    # 6. Cap at MAX_BUYS_PER_DAY
    return signals[:config.MAX_BUYS_PER_DAY]


if __name__ == "__main__":
    print("Running one-off scan...")
    candidates = scan()
    print(f"\nTop candidates: {len(candidates)}")
    for c in candidates:
        print(f"  {c['symbol']}: score={c['score']:+d} dir={c['direction']}")
