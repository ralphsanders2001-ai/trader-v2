
"""
Trader V2 — Centralized Configuration
=====================================

ALL tunables live here. NOTHING should be hardcoded in scripts.
Edit this file or use the dashboard to change any value at runtime.

Last loaded: daemon uses mtime check to hot-reload every 5s.
"""

# Auto-updated by dashboard (applied after TIER_PROFIT_TARGETS is defined below)

# =============================================================================
# PATHS
# =============================================================================
BASE_DIR          = "/home/ralph/trader-v2"
DATA_DIR          = f"{BASE_DIR}/data"
LOG_DIR           = f"{BASE_DIR}/logs"
DB_PATH           = f"{DATA_DIR}/trader_paper.db"  # 2026-08-10: separate paper/live DBs
DASHBOARD_HOST    = "0.0.0.0"
DASHBOARD_PORT    = 8091

# =============================================================================
# TIMING (all in seconds)
# =============================================================================
POLL_TIERS = {
    "high_velocity":   5,    # volatile symbols near thresholds
    "standard":       10,    # most positions
    "low_volatility": 15,    # calm symbols far from exits
}
SCAN_INTERVAL = 212
RISK_CHECK_INTERVAL  = 60    # 1 min  — margin/concentration check
ACCOUNT_REFRESH      = 300   # 5 min  — buying power refresh
CONFIG_RELOAD_TICK   = 5     # how often daemon checks config.py for changes

# =============================================================================
# MARKET HOURS (ET)
# =============================================================================
NO_TRADE_START_HHMM = "09:30"
NO_TRADE_END_HHMM = "09:45"
ENABLE_NO_TRADE_WINDOW = False
MARKET_OPEN_HHMM       = "09:30"
MARKET_CLOSE_HHMM      = "16:00"
EOD_FLATTEN_HHMM       = "15:45"   # 3:45 PM — 15 min before close (was 3:55 PM)
TRADING_DAYS           = [0, 1, 2, 3, 4]  # Mon=0, Fri=4

# =============================================================================
# LOSS MANAGEMENT (sliding scale)
# =============================================================================
# Loss caps — tightened after paper showed -$393 across 12 trades.
# The previous caps ($60 / 30%) let every trade become a max loss because
# we entered illiquid options with wide spreads and 0DTE decay.
LOSS_CAP_PCT = 0.066
LOSS_CAP_DOLLAR = 40.0

TIER_LOSS_CAPS = {
    "high_velocity":   0.08,     # 8% stop on volatile — exit fast
    "standard":        0.12,     # 12% stop on normal
    "low_volatility":  0.25,     # 15% stop on calm
}

TIER_PROFIT_TARGETS = {
    "high_velocity":   0.15,     # FIX 2026-08-05: 15% take-profit on volatile — was 20%, hit too rarely
    "standard":        0.20,     # FIX 2026-08-05: 20% take-profit on normal — was 25%
    "low_volatility":  0.25,     # FIX 2026-08-05: 25% take-profit on calm — was 30%
}

# 2026-09-14 (Ralph): sell-off on EVERY entry — stamp target_exit_price =
# entry * (1 + PROFIT_TARGET_PCT) at position open. Edit here or via dashboard
# /settings; the daemon hot-reloads it every 5s. Loss/exits untouched.
PROFIT_TARGET_PCT = 0.07

# =============================================================================
# V2 EXIT RULES (FIX 2026-08-10 — user redesign)
# Dollar-based exit logic instead of %-based. Hold losers to let them recover.
# =============================================================================
# FIX 2026-08-12: Adaptive profit target.
# Cheap options (<$5 entry) → $10 target + $30 cap.
# Expensive options (>=$5 entry) → $20 target + $50 cap (gives them room to run).
MAX_PROFIT_DOLLAR = 59.46
TAKE_PROFIT_DOLLAR = 13.22
                              #   (a) recovered from a loss
                              #   (b) was up +$20 and dropped back to +$10
# FIX 2026-08-12: Dollar-adaptive loss cap (replaces 25% percent).
# Cap at $15 per contract, OR 20% of entry, whichever is SMALLER.
# This stops the bleeding on cheap options ($1.25 → max $0.25 loss = $25/trade)
# while still capping expensive options ($17 → $3.40 loss = $340 — too high).
# Use max($15, entry * 0.20) as the trigger threshold per contract.
LOSS_CAP_DOLLAR_V2   = 30.00   # FIX 2026-08-13: Raised from $15 to $30 — $15 was too tight for ~$1 options, fired after -$11 to -$15 instead of letting them breathe
LOSS_CAP_PCT_V2      = 0.20    # Max percent loss per contract (20%)
EOD_CUTOFF_HHMM      = "15:55" # Sell 0DTE positions 5 min before market close
# Reason for raising: 1:2 R:R ratio per article discipline. Backtest shows
# 25% targets on profitable subset yield +$1333 (vs +$589 at 12% targets).
# FIX 2026-08-04: Tested 10% targets — WR unchanged but P&L dropped -$307.
# Reverted to original 20-30% tier targets.

# =============================================================================
# POSITION LIMITS
# =============================================================================
MAX_HOLD_MINUTES = 60
MIN_HOLD_MINUTES = 1    # FIX 2026-08-05: 1-min grace period: 60s grace period before loss cap can fire (was 0, instant exits)
#                       # on profitable subset (MU, INTC, LCID, SPCX, SMCI).
#                       # Losers naturally hit this ceiling while winners ride up.
MAX_BUYS_PER_DAY = 100
MAX_OPEN_POSITIONS   = 2      # FIX 2026-08-05: was 3 — today had 4-5 concurrent, too much risk
POSITION_SIZE_CONTRACTS = 1

# FIX 2026-08-13: Options trading fees — round-trip estimate for 0DTE options
# Includes SEC fee (sell-side), FINRA TAF, and OCC fee. $0 commission.
FEE_PER_CONTRACT = 0.05

# =============================================================================
# COOLDOWN (progressive per symbol)
# =============================================================================
COOLDOWN_FIRST_LOSS_MINUTES = 1
COOLDOWN_SECOND_LOSS_MINUTES = 30
COOLDOWN_THIRD_LOSS_ACTION   = "BLOCK"  # after 3rd loss, block rest of day

# =============================================================================
# CIRCUIT BREAKER (daily)
# =============================================================================
MAX_DAILY_LOSS = 999999.0
DAILY_LOSS_PAUSE      = -300      # 2026-09-15 (Ralph): pause new entries when today net P&L <= this. 0 disables. (was hardcoded -200)
#                                 # With BP ~$889, a $100 day is 11% of buying power.
#                                 # Hitting that = strategy is broken, stop trading.
WARN_PCT_OF_LIMIT    = 0.80     # warn at 80% ($80)

# =============================================================================
# ML GATE (FIX 2026-08-06)
# =============================================================================
USE_ML_GATE = False
ML_SKIP_THRESHOLD    = 0.55     # FIX 2026-08-12: Raised from 0.42 → 0.55. Be MORE selective. Only take trades the model rates >= 55% likely to profit.
ML_MIN_TRAIN_TRADES  = 20       # Min closed trades before ML gate activates
USE_LLM_GATE = False
LLM_TIMEOUT_SEC      = 5        # Max seconds to wait for LLM response

# =============================================================================
# ROBINHOOD CONTEXT FILTERS (FIX 2026-08-06)
# =============================================================================
USE_CONTEXT_FILTERS  = True     # Earnings/news/VIX filters before ML gate
USE_ORDER_REVIEW     = True     # Tier 1: Simulate order before placing (review_option_order)
BLOCK_ON_REVIEW_WARNINGS = False  # If True, skip trades with wide-spread warnings (default: warn only)
USE_ROBINHOOD_MARKET_HOURS = True  # Tier 1: Use Robinhood API for market hours (handles holidays)
EARNINGS_BLOCK_DAYS  = 3        # Skip if earnings within N days
NEWS_BLOCK_KEYWORDS  = ["lawsuit", "investigation", "guidance cut", "miss",
                        "downgrade", "warning", "bankruptcy", "halt", "antitrust"]
VIX_CALL_BLOCK       = 25.0     # Skip calls when VIX > this (panic regime)
VIX_PUT_BLOCK        = 12.0     # Skip puts when VIX < this (complacency)

# =============================================================================
# PER-SYMBOL COOLDOWN (FIX 2026-08-07) — user wants diversity
# =============================================================================
# 2026-09-15 (Ralph): split into WIN and LOSS cooldowns, both tunable on the
# settings page. WIN = time before re-entering a symbol after a profitable
# close; LOSS = after a losing close (loss ladder stacks on top of this).
# A missing/legacy value falls back to the old combined 900s.
SYMBOL_COOLDOWN_WIN_SEC = 5      # after a WIN on a symbol (user set 18:06 Sep 15)
SYMBOL_COOLDOWN_LOSS_SEC = 900   # after a LOSS on a symbol (15 min, before ladder)
                                # after a closed trade. Forces diversification
                                # so the bot doesn't hammer NVDA all day.

# =============================================================================
# RSI EXIT (FIX 2026-08-06)
# =============================================================================
USE_RSI_EXIT         = True     # Exit on RSI overbought/oversold + divergence
RSI_OVERBOUGHT       = 75       # Long call: RSI > this + profit → exit
RSI_VERY_OVERBOUGHT  = 80       # Long call: RSI > this → exit regardless
RSI_OVERSOLD         = 25       # Long put: RSI < this + profit → exit
RSI_VERY_OVERSOLD    = 20       # Long put: RSI < this → exit regardless
RSI_PERIOD           = 14       # RSI calculation period
RSI_TIMEFRAME        = "5minute" # Candle timeframe
RSI_PROFIT_THRESHOLD = 0.08     # Min profit % for RSI exit (8%)

# =============================================================================
# CONFIDENCE SIZING
# =============================================================================
SIZE_MULTIPLIERS = {
    0:  1.0,    # no streak — base size
    1:  1.0,    # 1 win — still base
    2:  1.25,   # 2 wins — bump
    3:  1.5,    # 3+ wins — cap
}

# =============================================================================
# WATCHLIST / MOVERS
# =============================================================================
# 2026-08-11: User wants calls ONLY. Puts disabled.
ALLOWED_OPTION_TYPES = ["call"]  # Only buy calls. Set to ["call", "put"] to allow both.
# 2026-08-12: narrowed to SPY/QQQ/NVDA per user ("proven winners only")
# 2026-09-14: user asked to widen back to all 17 symbols from trade history
WATCHLIST = ["SPY", "QQQ", "NVDA", "AMD", "MU", "INTC", "AAPL", "AMZN", "BA", "AXTI", "LCID", "RBLX", "SPCX"]
TOP_MOVERS_COUNT          = 0   # disabled — watchlist only
TOP_MOVERS_REFRESH        = 300 # 5 min (matches SCAN_INTERVAL) - unused
USER_WATCHLIST_OVERRIDE   = []  # user can pin extra symbols here

# Per-symbol trading enable (dashboard settings checkboxes). Missing symbol = enabled.
SYMBOLS_DISABLED = ["INTC", "BA"]

# 2026-09-16: cap on simultaneously ACTIVE (enabled) symbols — settings-page knob.
# Scanner must never take more than this many enabled symbols at once (API budget).
MAX_ACTIVE_SYMBOLS = 12

# =============================================================================
# FORCED OVERRIDES (per symbol)
# =============================================================================
FORCED_TURBULENT_SYMBOLS = []
FORCED_TIER_OVERRIDES = {
    # "NVDA": "high_velocity",   # example: pin to volatile tier
}

# =============================================================================
# CANDLE REVIEW
# =============================================================================
PRIMARY_TIMEFRAME       = "5m"   # each candle = 5 minutes
PRIMARY_CANDLE_COUNT    = 50    # 50 * 5min = ~4 hours of history (enough for MACD)
TREND_TIMEFRAME         = "15m"
TREND_CANDLE_COUNT      = 20    # ~5 hours on 15m
ENABLED_TIMEFRAMES      = ["5m"] # hard list — NEVER 1m/2m
MIN_CANDLES_FOR_SIGNAL = 24
#                                 # Reason: need real MACD/RSI setup, not noise

# =============================================================================
# SIGNAL QUALITY FILTERS (rejects bad signals before they become trades)
# =============================================================================
MIN_SCORE_THRESHOLD     = 30
#                                 # Score is from -100 to +100. ±50 is "strong".
MIN_BID_ASK_SPREAD_PCT  = 0.15   # FIX 2026-08-05: was 0.50 — too loose, allowed wide-spread options that lost instantly
#                                 # e.g. bid=0.10, ask=0.20 → spread=0.67 → REJECT
#                                 # Tight spreads mean liquid options, real fills.
MIN_OPTION_PRICE        = 0.10   # skip options priced below 10¢
#                                 # Sub-10¢ options have huge % swings on 1¢ moves
MIN_OPTION_OPEN_INTEREST = 100   # need at least 100 OI for liquidity
MIN_OPTION_VOLUME       = 50     # need at least 50 contracts traded today
MAX_EXPIRY_DAYS         = 7      # skip 0DTE and 1DTE — gamma risk too high
#                                 # 2-7 DTE only — gives the signal room to play out

# =============================================================================
# V1 SIGNAL GENERATOR (adopted from /home/ralph/robinhood-trainer/signals.py)
# =============================================================================
# These are the V1 scoring weights. They produce scores 0-100 with stronger
# conviction than V2's broken ±65 formula.
SIGNAL_BASE_SCORE        = 50     # starting score
SCORE_EMA_TREND_ALL_ABOVE = 15    # +15 if price > EMA20 AND EMA50 AND EMA200
SCORE_GOLDEN_CROSS       = 10     # +10 if EMA50 > EMA200 (golden cross)
SCORE_BB_POSITION_BONUS  = 10     # +10 for call at lower band / put at upper band
SCORE_RSI_BONUS          = 10     # +10 for RSI in healthy range
SCORE_VOLUME_SURGE_BONUS = 5      # +5 for volume surge ratio > 1.5
SCORE_RESISTANCE_PENALTY = 15     # -15 for call near resistance / put near support
SCORE_EMA20_BELOW_PUT    = 15     # +15 for put if price < EMA20
SCORE_EMA50_BELOW_PUT    = 10     # +10 for put if price < EMA50
SCORE_PUT_RSI            = 10     # +10 for put if RSI > 60
MIN_SIGNAL_SCORE         = 55     # minimum score to enter a trade (V1's threshold)
PULLBACK_THRESHOLD       = 0.002  # 0.2% pullback from day high (V1 entry gate)
MAX_OTM_PCT              = 0.03   # 3% max OTM for option strike
MAX_BID_ASK_SPREAD_PCT   = 0.30   # 30% max bid-ask spread
MIN_OPEN_INTEREST        = 50     # 50 contracts min open interest
PREMIUM_TARGET_MULT      = 1.20   # +20% profit target on premium (reverted from 1.10 after negative backtest)
PREMIUM_STOP_MULT        = 0.70   # -30% stop on premium
UNDERLYING_STOP_MULT     = 0.993  # -0.7% underlying stop
UNDERLYING_TARGET_MULT   = 1.008  # +0.8% underlying target
VOLUME_SURGE_THRESHOLD   = 1.5    # volume vs avg ratio to count as surge (V1)
RSI_PERIOD               = 14     # RSI lookback
EMA_DEFAULT_SPAN         = 9      # default EMA span
EMA_FAST_PERIOD          = 20     # EMA20 for trend
EMA_MID_PERIOD           = 50     # EMA50 for trend
EMA_SLOW_PERIOD          = 200    # EMA200 for long-term trend
BB_PERIOD                = 20     # Bollinger band lookback
BB_STD_DEV               = 2      # Bollinger band std dev multiplier
VOLUME_LOOKBACK          = 20     # Volume surge lookback

# =============================================================================
# LLM (Ollama)
# =============================================================================
OLLAMA_HOST             = "http://127.0.0.1:11434"
OLLAMA_MODEL            = "llama3.2"
LLM_TIMEOUT_SECONDS     = 30
LLM_ENABLED             = True   # set False to skip LLM, use technical signals only

# =============================================================================
# API
# =============================================================================
ROBINHOOD_API_RATE_LIMIT = 100   # per minute (Robinhood's limit)
API_BUDGET_PCT           = 0.80  # stay under 80% of limit
API_BACKOFF_SECONDS      = 5     # when over budget, wait this long
API_RETRY_ATTEMPTS       = 3

# =============================================================================
# EXECUTION
# =============================================================================
# MODE: 'paper' = simulate orders, no real money, log to DB only
#       'live'  = real orders through Robinhood
# ALWAYS start in 'paper' after any code change. Switch to 'live' only after
# validating paper mode for at least one full trading day.
MODE = "paper"   # FIX 2026-08-10: switched to paper after losses — offline until further notice
ORDER_TYPE              = "limit"   # or "market"
LIMIT_PRICE_OFFSET = 0.01
BUY_LIMIT_OFFSET_PCT = 5.83

# Paper trading: starting balance, used for paper portfolio P&L calculation
PAPER_STARTING_BALANCE  = 1000.0   # virtual cash starting balance
DAILY_STARTING_BALANCE = 1000.0

# =============================================================================
# SHUTDOWN HYGIENE
# =============================================================================
CLEANUP_ON_STOP         = False     # DON'T close positions on stop — preserve across restarts (FIX 2026-08-10)

# =============================================================================
# LOGGING
# =============================================================================
LOG_LEVEL               = "INFO"
LOG_FILE                = f"{LOG_DIR}/daemon.log"
LOG_RETENTION_DAYS      = 30


# 2026-09-16: PER-SYMBOL OVERRIDES (settings page governs globals; these override per symbol)
# Keys: trading_hours "HH:MM-HH:MM" ET | buy_pct_offset | limit_offset | exit_pct (alias sell_off_pct)
#       | max_loss_pct | max_loss_dollar | take_profit | max_profit | hold_time_min
SYMBOL_SETTINGS = {'TSLA': {'buy_pct_offset': 15.0}}
