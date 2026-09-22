"""
Executor — places option orders via Robinhood MCP/robin_stocks.

Handles:
- Buy option (with tier-based sizing from win streak)
- Sell option (limit at bid + offset)
- Close all positions (shutdown hygiene)
- Cancel all pending orders (shutdown hygiene)

OPTIONS ONLY: This module never sells stocks. The execute_sell()
function and close_all_positions() both refuse to act on any row
missing option_type, option_strike, or option_expiry. The database
also has NOT NULL constraints on these columns.
"""
import logging
import sys
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/ralph/trader-v2")
sys.path.insert(0, "/home/ralph/trader-v2/scripts")
import config
from database import get_connection, log_signal
from robinhood_client import get_client
import scanner
try:
    from daemon import _sell_failure_counts, SELL_FAIL_GIVEUP_THRESHOLD
except ImportError:
    # Daemon not loaded (e.g. unit test). Use local fallback.
    _sell_failure_counts = {}
    SELL_FAIL_GIVEUP_THRESHOLD = 3

log = logging.getLogger("v2.executor")

ET = ZoneInfo("US/Eastern")


def get_win_streak_multiplier(symbol):
    """Return the size multiplier based on today's win streak."""
    conn = get_connection()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT consecutive_wins FROM symbol_daily_state WHERE symbol=? AND trading_day=?",
        (symbol, today)
    ).fetchone()
    conn.close()
    streak = row["consecutive_wins"] if row else 0
    # Pick highest tier that applies (3+ → 1.5)
    if streak >= 3:
        return config.SIZE_MULTIPLIERS[3]
    if streak >= 2:
        return config.SIZE_MULTIPLIERS[2]
    return config.SIZE_MULTIPLIERS.get(streak, 1.0)


def get_quantity_for_symbol(symbol):
    """Compute the number of contracts for a new entry on this symbol."""
    base = config.POSITION_SIZE_CONTRACTS
    multiplier = get_win_streak_multiplier(symbol)
    qty = int(base * multiplier)
    return max(1, qty)


def find_option_for_signal(symbol, direction):
    """
    Pick the best option contract for a direction signal.
    - Picks weekly expiry (today + 7 days if available, else closest)
    - Picks slightly OTM strike (1-2% above/below current price)
    Returns dict with symbol, expiry, strike, option_type, or None
    """
    client = get_client()
    quote = client.get_stock_quote(symbol)
    if not quote:
        return None
    price = quote["last"]
    if price <= 0:
        return None

    if direction == "long_call":
        # Slightly OTM call (1-2% above)
        target_strike = round(price * 1.01, 2)
        option_type = "call"
    elif direction == "long_put":
        # Slightly OTM put (1-2% below)
        target_strike = round(price * 0.99, 2)
        option_type = "put"
    else:
        return None

    # Find closest available strike to target
    try:
        # 2026-09-18: RH locked out — Alpaca options feed replaces robin_stocks
        import alpaca_options as ao
        # Get all available expiries for this symbol
        chains = ao.get_chains(symbol) or {}
        expiries = chains.get("expiration_dates", []) if isinstance(chains, dict) else []
        if not expiries:
            return None

        # Pick the closest expiry, BUT skip 0DTE and 1DTE (gamma risk too high).
        # config.MAX_EXPIRY_DAYS=7 means we look for expiries 2-7 days out.
        today = datetime.now(ET).date()
        min_dte = 2  # at least 2 days out
        max_dte = config.MAX_EXPIRY_DAYS
        valid_expiries = []
        for exp in expiries:
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if min_dte <= dte <= max_dte:
                    valid_expiries.append((dte, exp_date, exp))
            except Exception:
                continue
        if not valid_expiries:
            return None
        # Sort by DTE ascending — closest expiry first (still ≥2 days out)
        valid_expiries.sort(key=lambda x: x[0])
        chosen_expiry = valid_expiries[0][2]

        # Get nearest available strike to target using find_options_by_expiration
        # First list available strikes for this expiry+type
        options_list = ao.find_options_by_expiration(
            symbol, expirationDate=chosen_expiry, optionType=option_type, info=None
        ) or []
        if not options_list:
            return None

        # Find the strike closest to our target
        best = None
        best_diff = float('inf')
        for opt in options_list:
            try:
                strike = float(opt.get("strike_price", 0))
                diff = abs(strike - target_strike)
                if diff < best_diff:
                    best_diff = diff
                    best = opt
            except Exception:
                continue

        if not best:
            return None

        # Apply contract-quality filters:
        # 1. Open interest >= MIN_OPTION_OPEN_INTEREST
        # 2. Volume today >= MIN_OPTION_VOLUME
        # 3. Market data must show tight bid/ask spread (≤MIN_BID_ASK_SPREAD_PCT)
        # 4. Mid price >= MIN_OPTION_PRICE (skip sub-10¢)
        try:
            oi_raw = best.get("open_interest")
            # 2026-09-18: Alpaca snapshot feed does not provide open_interest.
            # If OI is missing (None), skip the OI gate and rely on volume/spread.
            # If OI is present, enforce it as before.
            if oi_raw is None:
                log.info(f"[EXEC] {symbol} ${strike}: OI unavailable from feed — skipping OI gate")
            else:
                oi = int(float(oi_raw or 0))
                if oi < config.MIN_OPTION_OPEN_INTEREST:
                    log.info(f"[EXEC] {symbol} ${strike} rejected: OI={oi} < {config.MIN_OPTION_OPEN_INTEREST}")
                    return None
            vol = int(float(best.get("volume", 0) or 0))
            if vol < config.MIN_OPTION_VOLUME:
                log.info(f"[EXEC] {symbol} ${strike} rejected: volume={vol} < {config.MIN_OPTION_VOLUME}")
                return None

            # Fetch market data to check spread and price
            market = client.get_option_market_data(
                symbol, chosen_expiry, float(best.get("strike_price", 0)), option_type
            )
            if not market or market.get("bid", 0) <= 0 or market.get("ask", 0) <= 0:
                log.info(f"[EXEC] {symbol} ${strike} rejected: no market data")
                return None

            bid = market["bid"]
            ask = market["ask"]
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid > 0 else 999

            if spread_pct > config.MIN_BID_ASK_SPREAD_PCT:
                log.info(f"[EXEC] {symbol} ${strike} rejected: spread={spread_pct:.1%} > {config.MIN_BID_ASK_SPREAD_PCT:.0%} "
                         f"(bid=${bid:.2f} ask=${ask:.2f})")
                return None
            if mid < config.MIN_OPTION_PRICE:
                log.info(f"[EXEC] {symbol} ${strike} rejected: mid=${mid:.2f} < ${config.MIN_OPTION_PRICE:.2f}")
                return None
        except Exception as e:
            log.warning(f"[EXEC] {symbol} ${strike} filter check failed: {e}, accepting anyway")
            # Don't reject on filter-check errors — let the order attempt proceed

        return {
            "symbol": symbol,
            "expiry": chosen_expiry,
            "strike": float(best.get("strike_price", 0)),
            "option_type": option_type,
            "instrument_id": best.get("id", ""),
        }
    except Exception as e:
        log.error(f"find_option_for_signal failed for {symbol}: {e}")
        return None


def execute_buy(signal):
    """
    Execute a buy order based on a signal.
    Returns the trade id (db row), or None on failure.

    FIX 2026-08-06: ML gate — predict probability of profitability before
    entering. Skip trade if probability < threshold. Models trained on
    historical trades learn which setups actually win vs lose.

    FIX 2026-08-06: LLM layer — when ML is uncertain, ask Phi-4-mini for
    a context-aware decision based on recent trade history.

    FIX 2026-08-06: Robinhood context filters — earnings, news, VIX.
    Skip trades with binary event risk or unfavorable macro regime.
    """
    # === ROBINHOOD CONTEXT FILTERS (FIX 2026-08-06) ===
    # Cheap checks first — earnings, news, VIX.
    if getattr(config, "USE_CONTEXT_FILTERS", True):
        try:
            from rh_context import check_all_context_filters
            ctx = check_all_context_filters(
                signal["symbol"],
                signal.get("direction", "long_call"),
            )
            if ctx["should_block"]:
                blocked_filter = next(
                    (d for d in ctx["reasons"] if d["blocked"]), None
                )
                print(f"[CTX] SKIP {signal['symbol']}: "
                      f"{blocked_filter['filter']}={blocked_filter['reason']}")
                log_signal(signal, status="rejected",
                          rejection_reason=f"ctx_{blocked_filter['filter']}")
                return None
        except Exception as e:
            print(f"[CTX] Filter failed ({e}), proceeding")

    # === PER-SYMBOL ENABLE SWITCH (dashboard settings checkboxes) ===
    if not scanner.is_symbol_enabled(signal["symbol"].upper()):
        print(f"[EXEC] SKIP {signal['symbol']}: symbol disabled on settings page")
        log_signal(signal, status="rejected", rejection_reason="symbol_disabled")
        return None

    # === PER-SYMBOL TRADING HOURS (2026-09-16; e.g. SPY trades 09:30-12:00 only) ===
    # Entry gate only — exits (loss caps, TP, EOD flatten) always run.
    import symbol_settings as _ss
    if not _ss.in_trading_hours(signal["symbol"].upper()):
        log_signal(signal, status="rejected", rejection_reason="outside_trading_hours")
        return None

    # === PER-SYMBOL COOLDOWN (FIX 2026-08-07; split WIN/LOSS 2026-09-15) ===
    # Block re-entering the same symbol shortly after its last closed trade.
    # WIN and LOSS cooldowns are separate user-tunable knobs (settings page):
    # the interval used depends on whether the last trade won or lost.
    _win_cd  = getattr(config, "SYMBOL_COOLDOWN_WIN_SEC", 900)
    _loss_cd = getattr(config, "SYMBOL_COOLDOWN_LOSS_SEC", 900)
    _any_cd = max(_win_cd, _loss_cd)
    if _any_cd > 0:
        try:
            from database import get_connection as _gc
            import time as _time
            _conn = _gc()
            sym = signal["symbol"].upper()
            cutoff = (
                datetime.now() - timedelta(
                    seconds=_any_cd
                )
            ).isoformat()
            recent = _conn.execute("""
                SELECT timestamp_close, pnl FROM trades
                WHERE symbol = ?
                  AND timestamp_close IS NOT NULL
                  AND timestamp_close > ?
                  AND pnl IS NOT NULL
                ORDER BY timestamp_close DESC LIMIT 1
            """, (sym, cutoff)).fetchone()
            _conn.close()
            if recent:
                last_close, last_pnl = recent
                age_sec = _time.time() - (
                    datetime.fromisoformat(last_close).timestamp()
                )
                # Pick the cooldown that applies to how the last trade ended
                cooldown_sec = _win_cd if last_pnl > 0 else _loss_cd
                remaining = cooldown_sec - age_sec
                if remaining > 0:
                    kind = "WIN" if last_pnl > 0 else "LOSS"
                    print(f"[COOLDOWN] SKIP {sym}: last {kind} closed {age_sec:.0f}s ago, "
                          f"{remaining:.0f}s remaining of {cooldown_sec}s cooldown")
                    log_signal(signal, status="rejected",
                          rejection_reason=f"cooldown_{sym}_{int(remaining)}s")
                    return None
        except Exception as e:
            print(f"[COOLDOWN] Check failed ({e}), proceeding")

    # === CAPITAL GOVERNOR (2026-09-21, Ralph): $190 account + realized profits.
    # A new entry may only spend available cash = starting balance + running
    # realized P&L - cash reserved by open positions. Applies to BOTH modes:
    # paper simulates the same discipline the live account will have.
    try:
        import capital as _capital
        _qty_est = get_quantity_for_symbol(signal["symbol"])
        _price_est = signal.get("estimated_price") or signal.get("price") or 0
        # Paper path fills at limit (ask*(1-offset)); live path bids below ask.
        # Estimate cost with the offset applied when known, else raw estimate.
        _off = getattr(config, "BUY_LIMIT_OFFSET_PCT", 0.0)
        _limit_est = round(_price_est * (1.0 - (_off / 100.0)), 2) if _price_est else 0
        _cost_est = _limit_est * 100 * _qty_est
        _allowed, _cap = _capital.gate_buy(_cost_est)
        if not _allowed:
            print(f"[CAPITAL] SKIP {signal['symbol']}: cost est ${_cost_est:.2f} > "
                  f"available ${_cap['available']:.2f} (balance ${_cap['balance']:.2f} = "
                  f"${_cap['starting_balance']:.2f} start + ${_cap['realized_pnl']:.2f} realized, "
                  f"reserved ${_cap['reserved']:.2f})")
            log_signal(signal, status="rejected", rejection_reason="insufficient_capital")
            return None
    except Exception as _e:
        # Capital module failure must never block trading silently in the
        # wrong direction — fail CLOSED (skip the trade) and log loudly.
        print(f"[CAPITAL] gate failed ({_e}) — skipping trade for safety")
        log_signal(signal, status="rejected", rejection_reason="capital_gate_error")
        return None

    # === ML GATE (FIX 2026-08-06) ===
    if getattr(config, "USE_ML_GATE", True):
        try:
            from ml_predict import predict_trade_profitability
            # Build a stub contract for ML prediction
            entry_price = signal.get("estimated_price", 1.00)
            strike = signal.get("strike", 100.0)
            score = signal.get("score", 0)
            ml_result = predict_trade_profitability(
                signal["symbol"], signal["direction"].replace("long_", ""),
                strike, entry_price, score
            )

            # === LLM OVERRIDE (FIX 2026-08-06) ===
            # If ML says SKIP with low confidence, OR if ML says BUY but
            # score is borderline, ask the LLM for a second opinion.
            should_ask_llm = (
                (ml_result["recommendation"] == "SKIP" and ml_result["confidence"] == "low") or
                (ml_result["recommendation"] == "BUY" and ml_result["confidence"] == "medium")
            )

            if should_ask_llm and getattr(config, "USE_LLM_GATE", True):
                try:
                    from llm_decide import llm_review_candidate
                    llm_result = llm_review_candidate(
                        signal["symbol"],
                        signal["direction"].replace("long_", ""),
                        strike, entry_price, score,
                        ml_result["probability"],
                        ml_result["recommendation"]
                    )
                    print(f"[LLM] {signal['symbol']} {signal['direction']}: "
                          f"{llm_result['decision']} ({llm_result['confidence']}) "
                          f"— {llm_result['reasoning']}")

                    # If LLM says SKIP, override ML
                    if llm_result["decision"] == "SKIP":
                        log_signal(signal, status="rejected",
                                  rejection_reason=f"llm_skip_{llm_result['confidence']}")
                        return None
                    # If LLM says BUY with high confidence, override ML skip
                    elif llm_result["decision"] == "BUY" and llm_result["confidence"] == "high":
                        print(f"[LLM] High-confidence BUY override: {llm_result['reasoning']}")
                        # Fall through to execute
                except Exception as e:
                    print(f"[LLM] LLM gate failed ({e}), falling back to ML")

            if ml_result["recommendation"] == "SKIP":
                print(f"[ML] SKIP {signal['symbol']} {signal['direction']}: "
                      f"P(profit)={ml_result['probability']:.1%} "
                      f"(conf={ml_result['confidence']})")
                log_signal(signal, status="rejected",
                          rejection_reason=f"ml_skip_p{ml_result['probability']:.2f}")
                return None
            else:
                print(f"[ML] BUY {signal['symbol']} {signal['direction']}: "
                      f"P(profit)={ml_result['probability']:.1%} "
                      f"(conf={ml_result['confidence']})")
        except Exception as e:
            print(f"[ML] ML gate failed ({e}), proceeding with trade")

    contract = find_option_for_signal(signal["symbol"], signal["direction"])
    if not contract:
        print(f"[EXEC] No contract found for {signal['symbol']} {signal['direction']}")
        log_signal(signal, status="rejected", rejection_reason="no_contract")
        return None

    # PAPER MODE: simulate the order using real-time market data.
    # No Robinhood API call. Synthetic order ID so the verification code
    # below runs the same path and the DB record is indistinguishable
    # from a live trade except for the order_id prefix.
    if config.MODE == "paper":
        import uuid as _uuid
        return _paper_execute_buy(signal, contract)

    client = get_client()
    market = client.get_option_market_data(
        contract["symbol"], contract["expiry"], contract["strike"], contract["option_type"]
    )
    if not market or market["ask"] <= 0:
        print(f"[EXEC] No market data for {contract}")
        log_signal(signal, status="rejected", rejection_reason="no_market_data")
        return None

    # Buy limit pricing:
    #   BUY_LIMIT_OFFSET_PCT > 0  ->  percentage BELOW ask (patient bid; may not fill)
    #   otherwise                 ->  ask + small offset (aggressive; fills fast)
    # 2026-09-16: per-symbol overrides buy_pct_offset / limit_offset (symbol_settings.py)
    import symbol_settings as _ss
    _sym_u = signal["symbol"].upper()
    buy_pct = _ss.get(_sym_u, "buy_pct_offset", getattr(config, "BUY_LIMIT_OFFSET_PCT", 0.0))
    _limit_off = _ss.get(_sym_u, "limit_offset", getattr(config, "LIMIT_PRICE_OFFSET", 0.0))
    if buy_pct and buy_pct > 0:
        limit_price = round(market["ask"] * (1.0 - buy_pct / 100.0), 2)
        print(f"[EXEC] Bid-below-ask: {buy_pct}% under ask -> limit ${limit_price} (ask ${market['ask']}, bid ${market['bid']})")
        if limit_price <= market["bid"]:
            print(f"[EXEC] WARNING: limit ${limit_price} at/below bid ${market['bid']} — only fills on a price drop")
    else:
        limit_price = round(market["ask"] + _limit_off, 2)
    quantity = get_quantity_for_symbol(signal["symbol"])

    # === CAPITAL GOVERNOR — exact-cost check (2026-09-21, Ralph) ===
    try:
        import capital as _capital
        _cost = limit_price * 100 * quantity
        _allowed, _cap = _capital.gate_buy(_cost)
        if not _allowed:
            print(f"[CAPITAL] SKIP LIVE BUY {contract['symbol']} ${contract['strike']}: "
                  f"cost ${_cost:.2f} > available ${_cap['available']:.2f}")
            log_signal(signal, status="rejected", rejection_reason="insufficient_capital")
            return None
        print(f"[CAPITAL] OK: cost ${_cost:.2f} <= available ${_cap['available']:.2f} "
              f"(balance ${_cap['balance']:.2f})")
    except Exception as _e:
        print(f"[CAPITAL] gate failed ({_e}) — skipping trade for safety")
        log_signal(signal, status="rejected", rejection_reason="capital_gate_error")
        return None

    print(f"[EXEC] Buying {contract['symbol']} ${contract['strike']} {contract['option_type']} "
          f"x{quantity} @ ${limit_price} (ask ${market['ask']})")

    # === TIER 1 SAFETY: REVIEW ORDER BEFORE PLACING (FIX 2026-08-08) ===
    # Simulate the order to catch bad pricing, wide spreads, or invalid contracts
    # before risking real money.
    if getattr(config, "USE_ORDER_REVIEW", True):
        try:
            preview = client.review_option_order(
                symbol=contract["symbol"],
                quantity=quantity,
                option_type=contract["option_type"],
                strike=contract["strike"],
                expiry=contract["expiry"],
                side="buy",
                price=limit_price,
            )
            if not preview.get("valid"):
                print(f"[REVIEW] SKIP {contract['symbol']} ${contract['strike']} {contract['option_type']}: {preview.get('error')}")
                log_signal(signal, status="rejected", rejection_reason=f"review_failed:{preview.get('error', 'invalid')}")
                return None

            # Check warnings (wide spread is the main one)
            warnings = preview.get("warnings", [])
            if warnings and getattr(config, "BLOCK_ON_REVIEW_WARNINGS", False):
                print(f"[REVIEW] BLOCKED {contract['symbol']}: warnings={warnings}")
                log_signal(signal, status="rejected", rejection_reason=f"review_warning:{warnings}")
                return None
            elif warnings:
                print(f"[REVIEW] WARN {contract['symbol']}: {warnings}")

            # Verify our limit price is reasonable vs current market
            mid = preview.get("mid", 0)
            ask = preview.get("ask", 0)
            if mid and limit_price > ask * 1.5:
                print(f"[REVIEW] BLOCKED {contract['symbol']}: limit ${limit_price} > 1.5x ask ${ask}")
                log_signal(signal, status="rejected", rejection_reason=f"price_too_high:{limit_price}_vs_{ask}")
                return None
        except Exception as e:
            print(f"[REVIEW] Review check failed ({e}), proceeding with caution")

    # Loud banner before placing a real-money order.
    print(f"[LIVE] >>> PLACING REAL BUY ORDER: {contract['symbol']} "
          f"${contract['strike']} {contract['option_type']} x{quantity} @ ${limit_price} <<<")

    if config.ORDER_TYPE == "market":
        # Use robin_stocks market order
        try:
            import robin_stocks.robinhood as r
            order = r.order_buy_option_limit(
                positionEffect="open", creditOrDebit="debit",
                symbol=contract["symbol"], expirationDate=contract["expiry"],
                strike=contract["strike"], optionType=contract["option_type"],
                quantity=quantity, limitPrice=limit_price,
            )
        except Exception as e:
            print(f"[EXEC] Market buy failed: {e}")
            log_signal(signal, status="rejected", rejection_reason=f"order_exception:{e}")
            return None
    else:
        order = client.order_buy_option_limit(
            symbol=contract["symbol"], expiry=contract["expiry"],
            strike=contract["strike"], option_type=contract["option_type"],
            quantity=quantity, limit_price=limit_price,
        )

    # VERIFY THE ORDER WAS ACCEPTED.
    # robin_stocks returns a dict on both success and failure. A success dict
    # has an 'id' (the Robinhood order ID). A failure dict has 'detail' instead.
    # Without this check, the DB happily records trades for orders Robinhood
    # actually rejected — that's how we got $250 in phantom losses today.
    if not order:
        print(f"[EXEC] Buy order failed: empty response")
        log_signal(signal, status="rejected", rejection_reason="empty_response")
        return None
    if not isinstance(order, dict):
        print(f"[EXEC] Buy order failed: unexpected response type {type(order).__name__}")
        log_signal(signal, status="rejected", rejection_reason=f"unexpected_type:{type(order).__name__}")
        return None
    order_id = order.get("id")
    if not order_id:
        detail = order.get("detail") or order.get("error") or str(order)[:200]
        print(f"[EXEC] Buy order REJECTED by Robinhood: {detail}")
        log_signal(signal, status="rejected", rejection_reason=f"order_rejected:{detail[:100]}")
        return None
    print(f"[EXEC] Buy order ACCEPTED: {order_id}")

    # FIX 2026-08-07: Wait briefly to confirm the order actually fills.
    # Robinhood can return an order_id for an order that never executes
    # (rate-limited, market moved, etc). If we record the trade without
    # a fill, we get "phantom" trades that are open in our DB but closed
    # on Robinhood — which orphan-sync then mis-handles.
    #
    # 2026-08-07 update: also pull actual fill price from executions and
    # detect partial fills.
    import time as _buy_time
    _buy_time.sleep(3)  # Give Robinhood time to fill
    try:
        status = client.get_order_status(order_id)
        fill_state = status.get("state", "").lower() if status else "unknown"
        filled_qty = status.get("filled_quantity", 0) if status else 0

        if fill_state == "filled":
            pending_qty = status.get("pending_quantity", 0) if status else 0
            ordered_qty = status.get("ordered_quantity", quantity) if status else quantity

            if filled_qty < quantity:
                # Partial fill — proceed but log warning
                print(f"[EXEC] PARTIAL fill {order_id}: {filled_qty}/{quantity} contracts (pending={pending_qty})")
                log_signal(signal, status="taken",
                          rejection_reason=f"partial:{filled_qty}/{quantity}")
            elif pending_qty > 0 and filled_qty == ordered_qty:
                # Order fully filled but pending still showing (clearing up)
                # This is normal — Robinhood still updating pending state
                pass
            actual_price = status.get("average_price", 0)
            if actual_price and abs(actual_price - limit_price) > 0.10:
                # Significant slippage — log it
                print(f"[EXEC] Fill slippage {order_id}: requested ${limit_price:.2f}, filled ${actual_price:.2f}")
        elif fill_state in ("cancelled", "rejected"):
            print(f"[EXEC] Buy order {order_id} state={fill_state} after 3s")
            log_signal(signal, status="rejected", rejection_reason=f"unfilled:{fill_state}")
            return None
        else:
            # Still pending, confirmed, or unknown — try to cancel
            print(f"[EXEC] Buy order {order_id} state={fill_state} after 3s — cancelling")
            try:
                client.cancel_option_order(order_id)
            except Exception:
                pass
            log_signal(signal, status="rejected", rejection_reason=f"unfilled:{fill_state}")
            return None
    except Exception as e:
        # If we can't verify, proceed but log the warning
        print(f"[EXEC] Could not verify fill for {order_id}: {e}")

    # Record trade in DB
    trade_id = record_trade_open(signal, contract, limit_price, quantity, order)

    # 2026-09-17 (Ralph): record bid placement audit trail (live path) —
    # market context at placement + placement time (order submit), fill time
    # = when the DB row was written after fill verification.
    try:
        conn = get_connection()
        conn.execute("""
            UPDATE trades
            SET bid_at_open=?, ask_at_open=?, bid_place_time=?, bid_place_price=?, bid_fill_time=?
            WHERE id=?
        """, (
            market.get("bid"), market.get("ask"),
            datetime.now(ET).isoformat(), limit_price,
            datetime.now(ET).isoformat(),
            trade_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[EXEC] bid-place audit logging failed for trade {trade_id}: {e}")

    log_signal(signal, status="taken", trade_id=trade_id)
    return trade_id


def record_trade_open(signal, contract, entry_price, quantity, order):
    """Insert a new trade + position row.

    `order` can be either:
    - a dict (live mode): must have 'id' from Robinhood
    - a dict with 'simulated' key (paper mode): synthetic order ID
    """
    order_id = order.get("id") if isinstance(order, dict) else None
    conn = get_connection()
    cur = conn.cursor()

    # 2026-09-14 (Ralph): on EVERY entry, arm a profit sell-off by stamping
    # target_exit_price = entry * PROFIT_TARGET_PCT (default +10%, tunable in
    # config.py / dashboard settings). The daemon's poll loop sells the
    # position when bid >= target (exit_reason 'target_exit').
    # Profit side ONLY — all loss/stop exits left as-is per instruction.
    # 2026-09-16: per-symbol override (symbol_settings.exit_pct / sell_off_pct)
    import symbol_settings as _ss
    _profit_target_pct = _ss.get(signal.get("symbol") if isinstance(signal, dict) else None,
                                 "exit_pct", getattr(config, "PROFIT_TARGET_PCT", 0.10))
    _target_exit = round(entry_price * (1.0 + _profit_target_pct), 2) if entry_price else None

    cur.execute("""
        INSERT INTO trades (timestamp_open, mode, symbol, option_type, option_strike,
                           option_expiry, quantity, entry_price, entry_signal_score,
                           entry_signal_components, entry_candles_used, robinhood_order_id,
                           placed_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(ET).isoformat(),
        config.MODE,  # 'live' or 'paper'
        contract["symbol"], contract["option_type"], contract["strike"],
        contract["expiry"], quantity, entry_price,
        signal["score"], str(signal["components"]),
        str(signal.get("candles_snapshot", [])),
        order_id,
        "bot",  # FIX 2026-08-10: bot records its own trades as 'bot' (was 'unknown' or wrongly attributed as 'user' by sync)
    ))
    trade_id = cur.lastrowid
    cur.execute("""
        INSERT INTO positions (symbol, option_type, option_strike, option_expiry,
                              quantity, entry_price, entry_time, robinhood_instrument_id,
                              tier, source, target_exit_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        contract["symbol"], contract["option_type"], contract["strike"],
        contract["expiry"], quantity, entry_price,
        datetime.now(ET).isoformat(),
        contract.get("instrument_id", ""),
        get_tier_for_symbol(contract["symbol"]),
        f"system_{config.MODE}",  # FIX 2026-08-10: differentiate paper vs live positions
        _target_exit,  # 2026-09-14: +10% profit sell-off armed on every entry
    ))
    conn.commit()
    conn.close()

    # 2026-09-15 (Ralph): on EVERY entry, immediately place the take-profit
    # sell order at the armed target (limit, GTC). Live: real GTC resting
    # order on Robinhood — fills the instant the premium touches the target,
    # no poll-loop dependency. Paper: simulated resting order — the daemon's
    # poll loop fills it when bid >= target (same trigger, same result).
    # Losing this order must not lose the trade: target_exit_price in the DB
    # remains armed and the poll loop still sells on trigger as backstop.
    if _target_exit:
        try:
            _place_target_exit_order(contract, quantity, _target_exit)
        except Exception as e:
            print(f"[TARGET] Failed to place take-profit sell for {contract['symbol']} "
                  f"(poll-loop backstop still armed @ ${_target_exit}): {e}")

    return trade_id


def _place_target_exit_order(contract, quantity, target_price):
    """Place the resting take-profit sell at target_price on entry."""
    if config.MODE == "paper":
        print(f"[TARGET] PAPER: take-profit sell armed at ${target_price} "
              f"(poll loop fills when bid >= target) for {contract['symbol']} "
              f"${contract['strike']} {contract['option_type']}")
        return
    client = get_client()
    order = client.order_sell_option_limit(
        symbol=contract["symbol"],
        expiry=contract["expiry"],
        strike=contract["strike"],
        option_type=contract["option_type"],
        quantity=quantity,
        limit_price=target_price,
        time_in_force="gtc",
    )
    if order and isinstance(order, dict) and order.get("id"):
        print(f"[TARGET] LIVE: GTC take-profit sell placed: {order.get('id')} "
              f"{contract['symbol']} ${contract['strike']} {contract['option_type']} "
              f"x{quantity} @ ${target_price}")
    else:
        detail = (order.get("detail") or order.get("error") or str(order)[:200]) if isinstance(order, dict) else str(order)[:200]
        raise RuntimeError(f"Robinhood rejected target sell: {detail}")


def get_tier_for_symbol(symbol):
    """Return the tier this symbol should be in."""
    if symbol in config.FORCED_TIER_OVERRIDES:
        return config.FORCED_TIER_OVERRIDES[symbol]
    if symbol in config.FORCED_TURBULENT_SYMBOLS:
        return "high_velocity"
    return "standard"


# ============================================================================
# PAPER MODE
# ============================================================================
# In paper mode we simulate orders locally. No Robinhood API call is made,
# but the rest of the trade lifecycle is identical: DB record, position
# tracking, exit rules, circuit breaker, P&L. This lets us validate the
# full strategy end-to-end without risking real money.

import uuid as _uuid


def _paper_order_id():
    """Generate a synthetic order ID for paper trades."""
    return f"paper-{_uuid.uuid4().hex[:12]}"


def _paper_pending_table():
    """Create the pending paper buys table if missing."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_pending_buys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            option_type TEXT NOT NULL,
            option_strike REAL NOT NULL,
            option_expiry TEXT NOT NULL,
            instrument_id TEXT,
            limit_price REAL NOT NULL,
            signal_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


def _paper_fill_open(signal, contract, fill_price, market):
    """Record an instant paper fill (shared by direct + pending-fill paths).

    Mirrors the old tail of _paper_execute_buy: record_trade_open + audit
    trail + log_signal. Returns trade id.

    PHANTOM GUARD (2026-09-21, Ralph): never record an instant fill whose
    limit sat below the current bid — that fill could not have happened
    without a price drop. The pending-buy queue is the only legal path for
    patient bids. This guard is the backstop if any code path skips the
    queue check.
    """
    bid = market.get("bid")
    if bid is not None and fill_price < bid - 1e-9:
        print(f"[PAPER] PHANTOM GUARD: refusing instant fill {contract['symbol']} "
              f"limit ${fill_price} < bid ${bid} — queue it instead")
        _paper_queue_pending_buy(signal, contract, fill_price, market)
        return None
    quantity = 1  # paper always 1 contract
    print(f"[PAPER] BUY {contract['symbol']} ${contract['strike']} {contract['option_type']} "
          f"x{quantity} @ ${fill_price} (ask ${market.get('ask')}, bid ${market.get('bid')})")

    order = {
        "id": _paper_order_id(),
        "simulated": True,
        "fill_price": fill_price,
    }

    trade_id = record_trade_open(signal, contract, fill_price, quantity, order)

    # 2026-09-17 (Ralph): record bid placement audit trail — the bid context at
    # order placement and the placement time itself. Paper path: placement is
    # the moment the limit price is computed; fill time is when the fill
    # actually happened (instant here, or when the pending order triggered).
    try:
        conn = get_connection()
        conn.execute("""
            UPDATE trades
            SET bid_at_open=?, ask_at_open=?, bid_place_time=?, bid_place_price=?, bid_fill_time=?
            WHERE id=?
        """, (
            market.get("bid"), market.get("ask"),
            datetime.now(ET).isoformat(), fill_price,
            datetime.now(ET).isoformat(),
            trade_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[EXEC] bid-place audit logging failed for trade {trade_id}: {e}")

    log_signal(signal, status="filled", trade_id=trade_id)
    print(f"[PAPER] BUY FILLED: trade_id={trade_id}, order_id={order['id']}")
    return trade_id


def _paper_queue_pending_buy(signal, contract, limit_price, market):
    """Limit sits below the current bid — a real fill requires the ask to
    drop to the limit. Queue a pending paper buy instead of pretending it
    filled (2026-09-21, Ralph: option 1 — realistic paper fills)."""
    _paper_pending_table()
    import json as _json
    conn = get_connection()
    # One patient bid per symbol: replace any older queued bid for this symbol
    # (the scanner re-candidates the symbol every cycle until it fills).
    conn.execute(
        "UPDATE paper_pending_buys SET active=0 WHERE active=1 AND symbol=?",
        (contract["symbol"],),
    )
    conn.execute("""
        INSERT INTO paper_pending_buys
            (created_at, symbol, option_type, option_strike, option_expiry,
             instrument_id, limit_price, signal_json, active)
        VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        contract["symbol"], contract["option_type"], contract["strike"],
        contract["expiry"], contract.get("instrument_id", ""), limit_price,
        _json.dumps(signal),
    ))
    conn.commit()
    conn.close()
    log_signal(signal, status="pending_buy",
               rejection_reason=f"queued_below_bid:{limit_price}")
    print(f"[PAPER] PENDING BUY queued: {contract['symbol']} ${contract['strike']} "
          f"{contract['option_type']} limit ${limit_price} < bid ${market.get('bid')} — "
          f"fills only if ask drops to the limit")


def check_pending_paper_buys():
    """Daemon hook: try to fill queued paper buys whose ask has dropped to
    the limit. Called from the daemon poll loop each cycle."""
    try:
        _paper_pending_table()
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM paper_pending_buys WHERE active=1 ORDER BY id"
        ).fetchall()
        conn.close()
        if not rows:
            return
    except Exception as e:
        print(f"[PAPER] pending-buys check failed: {e}")
        return

    client = get_client()
    import json as _json
    for row in rows:
        try:
            # Cancel if a position in this symbol is already open (scanner
            # rule: one position per symbol).
            conn = get_connection()
            dup = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE symbol=?", (row["symbol"],)
            ).fetchone()[0]
            conn.close()
            if dup:
                conn = get_connection()
                conn.execute("UPDATE paper_pending_buys SET active=0 WHERE id=?", (row["id"],))
                conn.commit()
                conn.close()
                print(f"[PAPER] PENDING BUY cancelled {row['symbol']}: position already open")
                continue

            market = client.get_option_market_data(
                row["symbol"], row["option_expiry"], row["option_strike"], row["option_type"]
            )
            if not market or market.get("ask", 0) <= 0:
                continue  # keep waiting; no data this cycle
            if market["ask"] > row["limit_price"]:
                continue  # still waiting for the price to come to us

            # Fill: entry at the limit (that's where our resting bid sits).
            contract = {
                "symbol": row["symbol"],
                "option_type": row["option_type"],
                "strike": row["option_strike"],
                "expiry": row["option_expiry"],
                "instrument_id": row["instrument_id"] or "",
            }
            signal = _json.loads(row["signal_json"])
            # Capital re-check at fill time (cash may have moved since queued).
            try:
                import capital as _capital
                _allowed, _cap = _capital.gate_buy(row["limit_price"] * 100)
                if not _allowed:
                    conn = get_connection()
                    conn.execute("UPDATE paper_pending_buys SET active=0 WHERE id=?", (row["id"],))
                    conn.commit()
                    conn.close()
                    log_signal(signal, status="rejected",
                               rejection_reason="pending_insufficient_capital")
                    print(f"[PAPER] PENDING BUY cancelled {row['symbol']}: capital "
                          f"${_cap['available']:.2f} < cost ${row['limit_price']*100:.2f}")
                    continue
            except Exception as _e:
                print(f"[CAPITAL] pending gate failed ({_e}) — cancelling pending buy")
                conn = get_connection()
                conn.execute("UPDATE paper_pending_buys SET active=0 WHERE id=?", (row["id"],))
                conn.commit()
                conn.close()
                continue

            # Audit trail: placement = when queued; fill = now.
            signal2 = dict(signal)
            signal2["bid_place_time"] = row["created_at"]
            signal2["bid_place_price"] = row["limit_price"]
            trade_id = _paper_fill_open(signal2, contract, row["limit_price"], market)
            conn = get_connection()
            conn.execute("UPDATE paper_pending_buys SET active=0 WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
            # Audit columns for the pending fill: bid_place_time was queue time.
            try:
                conn = get_connection()
                conn.execute("""
                    UPDATE trades SET bid_place_time=?, bid_place_price=?, bid_fill_time=?
                    WHERE id=?
                """, (row["created_at"], row["limit_price"], datetime.now(ET).isoformat(), trade_id))
                conn.commit()
                conn.close()
            except Exception:
                pass
        except Exception as e:
            print(f"[PAPER] pending-buy {row['id']} processing failed: {e}")


def cancel_pending_paper_buys(reason="eod"):
    """Deactivate all pending paper buys (EOD cutoff / manual)."""
    try:
        _paper_pending_table()
        conn = get_connection()
        n = conn.execute("SELECT COUNT(*) FROM paper_pending_buys WHERE active=1").fetchone()[0]
        if n:
            conn.execute("UPDATE paper_pending_buys SET active=0 WHERE active=1")
            conn.commit()
            print(f"[PAPER] cancelled {n} pending buy(s) ({reason})")
        conn.close()
    except Exception as e:
        print(f"[PAPER] pending cancel failed: {e}")


def _paper_execute_buy(signal, contract):
    """Paper mode: realistic limit-fill simulation.

    2026-09-21 (Ralph, option 1): if the computed limit sits BELOW the current
    bid, a real resting order would not fill until the ask dropped to it — so
    the buy is queued as a pending paper buy instead of pretending. Only a
    marketable limit (limit >= bid, i.e. at/above the bid inside the spread or
    at/above the ask) fills instantly, at the limit price.
    """
    client = get_client()  # still used for market data (read-only)
    market = client.get_option_market_data(
        contract["symbol"], contract["expiry"], contract["strike"], contract["option_type"]
    )
    if not market or market.get("ask", 0) <= 0:
        print(f"[PAPER] No market data for {contract['symbol']} ${contract['strike']}")
        log_signal(signal, status="rejected", rejection_reason="no_market_data")
        return None

    # Paper fill honors BUY_LIMIT_OFFSET_PCT (patient bid) so paper simulates
    # the live path exactly: limit = ask x (1 - pct/100). When pct=0 this
    # reduces to fill-at-ask.
    buy_pct = getattr(config, "BUY_LIMIT_OFFSET_PCT", 0.0)
    if buy_pct > 0:
        fill_price = round(market["ask"] * (1.0 - buy_pct / 100.0), 2)
    else:
        fill_price = round(market["ask"], 2)
    quantity = 1  # paper always 1 contract

    # === REALISTIC FILL RULE ===
    if fill_price < market["bid"]:
        # Unfillable right now — queue it; daemon fills when ask <= limit.
        _paper_queue_pending_buy(signal, contract, fill_price, market)
        return None

    # === CAPITAL GOVERNOR — exact-cost check (2026-09-21, Ralph) ===
    # The estimated gate in execute_buy uses signal price; here the real fill
    # price is known, so enforce with the true contract cost.
    try:
        import capital as _capital
        _cost = fill_price * 100 * quantity
        _allowed, _cap = _capital.gate_buy(_cost)
        if not _allowed:
            print(f"[CAPITAL] SKIP PAPER BUY {contract['symbol']} ${contract['strike']}: "
                  f"cost ${_cost:.2f} > available ${_cap['available']:.2f} "
                  f"(balance ${_cap['balance']:.2f}, reserved ${_cap['reserved']:.2f})")
            log_signal(signal, status="rejected", rejection_reason="insufficient_capital")
            return None
        print(f"[CAPITAL] OK: cost ${_cost:.2f} <= available ${_cap['available']:.2f} "
              f"(balance ${_cap['balance']:.2f} = ${_cap['starting_balance']:.2f} + "
              f"${_cap['realized_pnl']:.2f} realized)")
    except Exception as _e:
        print(f"[CAPITAL] gate failed ({_e}) — skipping trade for safety")
        log_signal(signal, status="rejected", rejection_reason="capital_gate_error")
        return None

    return _paper_fill_open(signal, contract, fill_price, market)



def _paper_execute_sell(pos, reason):
    """Paper mode: simulate sell at current bid, record in DB."""
    client = get_client()  # market data only
    market = client.get_option_market_data(
        pos["symbol"], pos["option_expiry"], pos["option_strike"], pos["option_type"]
    )
    if not market or market.get("bid", 0) <= 0:
        print(f"[PAPER] No market data for sell: {pos['symbol']}")
        return False

    fill_price = round(market["bid"], 2)
    quantity = pos["quantity"]
    pnl = (fill_price - pos["entry_price"]) * quantity * 100
    fees = getattr(config, "FEE_PER_CONTRACT", 0.05) * quantity
    net_pnl = pnl - fees

    print(f"[PAPER] SELL {pos['symbol']} ${pos['option_strike']} {pos['option_type']} "
          f"x{quantity} @ ${fill_price} (bid ${market['bid']}) pnl=${pnl:+.2f} fees=${fees:.2f} net=${net_pnl:+.2f}")

    sell_order_id = _paper_order_id()
    _now = datetime.now(ET).isoformat()

    conn = get_connection()
    conn.execute("""
        UPDATE trades
        SET timestamp_close=?, exit_price=?, pnl=?, fees=?, net_pnl=?, exit_reason=?, exit_order_id=?,
            sell_place_time=?, sell_place_price=?
        WHERE id=(SELECT id FROM trades
                  WHERE symbol=? AND option_strike=? AND option_expiry=? AND option_type=?
                  AND exit_price IS NULL
                  ORDER BY timestamp_open DESC LIMIT 1)
    """, (
        _now, fill_price, pnl, fees, net_pnl, reason, sell_order_id,
        _now, fill_price,  # 2026-09-17: sell placement audit (paper: placement = fill)
        pos["symbol"], pos["option_strike"], pos["option_expiry"], pos["option_type"],
    ))
    conn.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
    update_symbol_state_after_trade(pos["symbol"], net_pnl, conn=conn)
    update_circuit_breaker_pnl(net_pnl, conn=conn)
    conn.commit()

    # FIX 2026-08-13: log per-trade ML feature contributions for diagnostics.
    # Best-effort — failure must not block the close.
    try:
        # Find the trade_id we just closed
        _tid_row = conn.execute("""
            SELECT id FROM trades
            WHERE symbol=? AND option_strike=? AND option_expiry=? AND option_type=?
              AND exit_reason=?
              AND timestamp_close=?
            ORDER BY id DESC LIMIT 1
        """, (pos["symbol"], pos["option_strike"], pos["option_expiry"], pos["option_type"],
              reason, datetime.now(ET).isoformat())).fetchone()
        if _tid_row:
            from trade_outcomes_logger import log_trade_outcome
            log_trade_outcome(_tid_row[0])
    except Exception as _e:
        print(f"[EXEC] trade_outcome logging failed: {_e}")

    conn.close()
    return True


def _get_position(position_id):
    """Fetch a position by ID. Returns dict or None."""
    conn = get_connection()
    pos = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
    conn.close()
    if pos:
        return dict(pos)
    return None


def execute_sell(position_id, reason="manual"):
    """
    Execute a sell order for a position. Returns True on success.
    Updates DB with exit price, pnl, and removes from positions.

    OPTIONS ONLY — this function will refuse to sell anything without
    option_type, option_strike, and option_expiry fields. Stocks
    (equity holdings like MSFT, SPCX) are never touched.
    """
    # SAFETY GUARD: refuse to sell anything that isn't an option contract.
    # This protects against any future code that might mistakenly insert
    # a stock holding into the positions table.
    pos = _get_position(position_id)
    if not pos:
        log.error(f"Sell rejected: position {position_id} not found")
        return False
    if not all([pos.get("option_type"), pos.get("option_strike"), pos.get("option_expiry")]):
        log.error(
            f"SELL REFUSED: position {position_id} has no option contract details. "
            f"This system ONLY trades options and never sells stocks."
        )
        return False
    # Don't open conn yet — we need to call the API first to get the limit
    # price. Opening conn now would hold a DB connection during the network
    # round-trip, blocking other writers with "database is locked" errors.

    # Recursion guard: if we're already in execute_sell on this thread,
    # skip the circuit_breaker recursive close (it would deadlock).
    import threading as _t
    _in_sell = getattr(_t.current_thread(), '_in_execute_sell', False)
    _t.current_thread()._in_execute_sell = True
    try:
        # PAPER MODE: simulate the sell with no Robinhood API call.
        if config.MODE == "paper":
            return _paper_execute_sell(pos, reason)
        return _do_execute_sell(position_id, pos, reason)
    finally:
        _t.current_thread()._in_execute_sell = _in_sell


def _do_execute_sell(position_id, pos, reason):
    """Internal: actual sell logic, wrapped by execute_sell with recursion guard."""
    client = get_client()
    market = client.get_option_market_data(
        pos["symbol"], pos["option_expiry"], pos["option_strike"], pos["option_type"]
    )
    if not market or market["bid"] <= 0:
        print(f"[EXEC] No market data for sell: {pos['symbol']} ${pos['option_strike']}")
        return False

    limit_price = round(max(market["bid"] - config.LIMIT_PRICE_OFFSET, 0.01), 2)
    quantity = pos["quantity"]

    print(f"[EXEC] Selling {pos['symbol']} ${pos['option_strike']} {pos['option_type']} "
          f"x{quantity} @ ${limit_price} (bid ${market['bid']}) reason={reason}")

    # Loud banner before placing a real-money order.
    print(f"[LIVE] >>> PLACING REAL SELL ORDER: {pos['symbol']} "
          f"${pos['option_strike']} {pos['option_type']} x{quantity} @ ${limit_price} <<<")

    order = client.order_sell_option_limit(
        symbol=pos["symbol"], expiry=pos["option_expiry"],
        strike=pos["option_strike"], option_type=pos["option_type"],
        quantity=quantity, limit_price=limit_price,
    )

    # VERIFY THE SELL ORDER WAS ACCEPTED.
    # Same bug class as the buy: a dict with 'detail' but no 'id' means
    # the order was rejected. We must NOT record the trade as closed in
    # that case — the position is still open on Robinhood.
    if not order:
        print(f"[EXEC] Sell order failed: empty response")
        return False
    if not isinstance(order, dict):
        print(f"[EXEC] Sell order failed: unexpected response type {type(order).__name__}")
        return False
    sell_order_id = order.get("id")
    if not sell_order_id:
        detail = order.get("detail") or order.get("error") or str(order)[:200]
        print(f"[EXEC] Sell order REJECTED by Robinhood: {detail}")
        # FIX 2026-08-07: Track consecutive rejections. If Robinhood keeps
        # rejecting (e.g. position doesn't exist), give up after threshold
        # instead of spamming forever. Caller will mark as orphan on giveup.
        _fail_count = _sell_failure_counts.get(position_id, 0) + 1
        _sell_failure_counts[position_id] = _fail_count
        if _fail_count >= SELL_FAIL_GIVEUP_THRESHOLD:
            print(f"[EXEC] Sell rejected {_fail_count}x for position {position_id} "
                  f"— giving up (likely orphan, sync will clean up)")
            # Mark as orphan so next sync_positions_with_robinhood call removes it
            pos["_orphan"] = True
        return False
    print(f"[EXEC] Sell order ACCEPTED: {sell_order_id}")

    # FIX 2026-08-05: WAIT FOR FILL CONFIRMATION before updating DB.
    # Previously, "ACCEPTED" meant Robinhood took the limit order, but
    # the order could sit unfilled if price moved away. This caused the
    # dashboard to show open positions that were already closed on RH.
    # Now we poll the order status until it's filled (or timeout).
    import time as _time
    max_wait = 30  # seconds
    poll_interval = 2
    waited = 0
    fill_state = None
    fill_price_actual = limit_price
    while waited < max_wait:
        try:
            order_status = client.get_order_status(sell_order_id)
            if order_status:
                fill_state = order_status.get("state", "").lower()
                if fill_state == "filled":
                    # Get actual fill price from order info
                    fill_price_actual = order_status.get("average_price", limit_price) or limit_price
                    print(f"[EXEC] Sell order FILLED: ${fill_price_actual:.2f} after {waited}s")
                    break
                elif fill_state in ("cancelled", "rejected", "failed"):
                    print(f"[EXEC] Sell order {fill_state} — position still open on Robinhood")
                    return False
            # Bid may move — adjust limit price if needed
            if waited > 5 and fill_state != "filled":
                # Check current bid and possibly re-quote
                current_market = client.get_option_market_data(
                    pos["symbol"], pos["option_expiry"], pos["option_strike"], pos["option_type"]
                )
                if current_market and current_market.get("bid", 0) > 0:
                    new_limit = round(current_market["bid"] - 0.01, 2)
                    if new_limit < limit_price:
                        # Cancel old, place new at better price
                        print(f"[EXEC] Canceling {sell_order_id} to re-quote at ${new_limit}")
                        client.cancel_option_order(sell_order_id)
                        _time.sleep(1)
                        order2 = client.order_sell_option_limit(
                            symbol=pos["symbol"], expiry=pos["option_expiry"],
                            strike=pos["option_strike"], option_type=pos["option_type"],
                            quantity=quantity, limit_price=new_limit,
                        )
                        if order2 and order2.get("id"):
                            sell_order_id = order2["id"]
                            fill_price_actual = new_limit
                            print(f"[EXEC] Re-quoted sell: {sell_order_id} @ ${new_limit}")
        except Exception as e:
            print(f"[EXEC] Poll error (continuing): {e}")
        _time.sleep(poll_interval)
        waited += poll_interval

    if fill_state != "filled":
        print(f"[EXEC] Sell order {sell_order_id} did not fill within {max_wait}s — DB NOT updated")
        print(f"[EXEC] Position remains open on Robinhood until order fills or expires")
        return False

    # Now open conn right before the DB writes — after the API call has
    # already placed the order on Robinhood. Holding the conn only during
    # the actual writes minimizes lock contention.
    conn = get_connection()

    # Update trade row
    exit_price = fill_price_actual
    pnl = (exit_price - pos["entry_price"]) * quantity * 100
    fees = getattr(config, "FEE_PER_CONTRACT", 0.05) * quantity
    net_pnl = pnl - fees
    conn.execute("""
        UPDATE trades
        SET timestamp_close=?, exit_price=?, pnl=?, fees=?, net_pnl=?, exit_reason=?, exit_order_id=?,
            sell_place_time=?, sell_place_price=?
        WHERE id=(SELECT id FROM trades
                  WHERE symbol=? AND option_strike=? AND option_expiry=? AND option_type=?
                  AND exit_price IS NULL
                  ORDER BY timestamp_open DESC LIMIT 1)
    """, (
        datetime.now(ET).isoformat(), exit_price, pnl, fees, net_pnl, reason, sell_order_id,
        datetime.now(ET).isoformat(), limit_price,  # 2026-09-17: sell placement audit (live)
        pos["symbol"], pos["option_strike"], pos["option_expiry"], pos["option_type"],
    ))
    conn.execute("DELETE FROM positions WHERE id=?", (position_id,))

    # Update symbol daily state (uses net_pnl so streak math reflects fees)
    update_symbol_state_after_trade(pos["symbol"], net_pnl, conn=conn)

    # Update circuit breaker realized PnL (uses net_pnl)
    update_circuit_breaker_pnl(net_pnl, conn=conn)

    conn.commit()
    conn.close()
    return True


def update_symbol_state_after_trade(symbol, pnl, conn=None):
    """Apply win/loss streak logic after a trade closes.

    Uses the passed-in conn if provided (to avoid opening a second connection
    inside the same transaction — which would deadlock against itself). Falls
    back to a new conn only if none is passed (e.g. when called standalone).
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM symbol_daily_state WHERE symbol=? AND trading_day=?",
        (symbol, today)
    ).fetchone()
    if not row:
        conn.execute("""
            INSERT INTO symbol_daily_state (symbol, trading_day)
            VALUES (?, ?)
        """, (symbol, today))
        row = conn.execute(
            "SELECT * FROM symbol_daily_state WHERE symbol=? AND trading_day=?",
            (symbol, today)
        ).fetchone()

    if pnl > 0:
        new_wins = row["wins_today"] + 1
        new_losses = row["losses_today"]  # unchanged on a win
        new_consec_wins = row["consecutive_wins"] + 1
        new_consec_losses = 0
        cooldown_until = None
        blocked = 0
    elif pnl < 0:
        new_losses = row["losses_today"] + 1
        new_consec_losses = row["consecutive_losses"] + 1
        new_consec_wins = 0

        if new_losses == 1:
            cooldown_min = config.COOLDOWN_FIRST_LOSS_MINUTES
            until = datetime.now(ET) + timedelta(minutes=cooldown_min)
            cooldown_until = until.isoformat()
            blocked = 0
        elif new_losses == 2:
            cooldown_min = config.COOLDOWN_SECOND_LOSS_MINUTES
            until = datetime.now(ET) + timedelta(minutes=cooldown_min)
            cooldown_until = until.isoformat()
            blocked = 0
        else:
            # 3rd loss: block for rest of day
            cooldown_until = None
            blocked = 1
        new_wins = row["wins_today"]
    else:
        # breakeven, no change
        new_wins = row["wins_today"]
        new_losses = row["losses_today"]
        new_consec_wins = row["consecutive_wins"]
        new_consec_losses = row["consecutive_losses"]
        cooldown_until = row["cooldown_until"]
        blocked = row["blocked_for_day"]

    conn.execute("""
        UPDATE symbol_daily_state
        SET wins_today=?, losses_today=?, consecutive_wins=?, consecutive_losses=?,
            cooldown_until=?, blocked_for_day=?, realized_pnl=realized_pnl+?,
            last_trade_time=?
        WHERE symbol=? AND trading_day=?
    """, (
        new_wins, new_losses, new_consec_wins, new_consec_losses,
        cooldown_until, blocked, pnl, datetime.now(ET).isoformat(),
        symbol, today,
    ))
    if own_conn:
        conn.commit()
        conn.close()


def update_circuit_breaker_pnl(pnl_change, conn=None):
    """Update daily realized P&L and trip breaker if limit hit.

    Uses passed-in conn if available to avoid opening a second connection
    that would deadlock against the caller's open transaction.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    row = conn.execute("SELECT * FROM circuit_breaker WHERE trading_day=?", (today,)).fetchone()
    if not row:
        conn.execute("INSERT INTO circuit_breaker (trading_day) VALUES (?)", (today,))
        row = conn.execute("SELECT * FROM circuit_breaker WHERE trading_day=?", (today,)).fetchone()

    new_pnl = row["realized_pnl"] + pnl_change
    loss = abs(min(0, new_pnl))
    tripped = 1 if (loss >= config.MAX_DAILY_LOSS and not row["tripped"]) else row["tripped"]
    tripped_at = datetime.now(ET).isoformat() if tripped and not row["tripped"] else row["tripped_at"]
    reason = f"Daily loss ${loss:.2f} >= ${config.MAX_DAILY_LOSS}" if tripped and not row["tripped"] else row["reason"]

    conn.execute("""
        UPDATE circuit_breaker
        SET realized_pnl=?, tripped=?, tripped_at=?, reason=?, last_updated=?
        WHERE trading_day=?
    """, (new_pnl, tripped, tripped_at, reason, datetime.now(ET).isoformat(), today))
    if own_conn:
        conn.commit()
        conn.close()

    if tripped and not row["tripped"]:
        print(f"[CIRCUIT] TRIPPED at ${loss:.2f}")
        # Close all positions immediately — but only if we're not already
        # in the middle of a sell (would deadlock on the shared conn).
        # The thread-local flag is set by execute_sell at the top.
        import threading as _t
        if not getattr(_t.current_thread(), '_in_execute_sell', False):
            _t.current_thread()._in_execute_sell = True
            try:
                cb_conn = get_connection()
                positions = cb_conn.execute("SELECT * FROM positions").fetchall()
                cb_conn.close()
                for pos in positions:
                    execute_sell(pos["id"], reason="circuit_breaker")
            finally:
                _t.current_thread()._in_execute_sell = False


def cancel_all_pending_orders():
    """Cancel all pending orders (shutdown hygiene)."""
    client = get_client()
    pending = client.get_pending_option_orders()
    cancelled = 0
    failed = 0
    for o in pending:
        if client.cancel_option_order(o["id"]):
            cancelled += 1
            print(f"  ✓ Cancelled order {o['id']}")
        else:
            failed += 1
            print(f"  ✗ Failed to cancel {o['id']}")
    print(f"[CLEANUP] Cancelled {cancelled} orders, {failed} failed")
    return cancelled, failed


def close_all_positions(reason="shutdown_cleanup"):
    """
    Close all open positions (shutdown hygiene).

    OPTIONS ONLY — even if a row somehow lacks option contract details
    (which shouldn't happen), it will be skipped, not sold.

    FIX 2026-08-06: Respect on_hold flag — held positions are NEVER auto-closed,
    even on shutdown. Only the end-of-day cron (eod_flatten) overrides hold;
    user-initiated pauses preserve held positions.
    """
    conn = get_connection()
    positions = conn.execute("SELECT * FROM positions").fetchall()
    conn.close()

    closed = 0
    failed = 0
    skipped = 0
    held_preserved = 0
    for pos in positions:
        # Defensive check: never sell a non-option position
        if not all([pos["option_type"], pos["option_strike"], pos["option_expiry"]]):
            log.warning(f"Skipping position {pos['id']}: not an option contract")
            skipped += 1
            continue

        # FIX 2026-08-06: skip on_hold positions unless this is the EOD cron flatten.
        # User-initiated pauses must preserve held positions; the cron at 4:05 PM
        # is the only auto-flatten that overrides hold.
        on_hold = pos["on_hold"] if pos["on_hold"] else 0
        if on_hold and reason != "eod_flatten":
            log.info(f"[HOLD] Preserving {pos['symbol']} ${pos['option_strike']} "
                     f"(on_hold=1, reason={reason})")
            held_preserved += 1
            continue

        if execute_sell(pos["id"], reason=reason):
            closed += 1
        else:
            failed += 1
    log.info(f"[CLEANUP] Closed {closed}, failed {failed}, skipped {skipped}, "
             f"preserved_held={held_preserved} (reason={reason})")
    return closed, failed
