"""
Robinhood client for Trader V2.

Handles login, 2FA challenge, session persistence, and order execution.
- Uses ~/.tokens/robinhood_v2.pickle for session persistence (24h)
- On startup: try pickle first, fall back to fresh login with 2FA prompt
- All API calls rate-limited to stay under Robinhood's 100/min limit

Tomorrow morning: the first time you start the daemon, it will:
1. Try pickle → fail (expired)
2. Call r.login() without mfa_code → Robinhood sends push to your phone
3. Approve on phone
4. Wait for the user to provide the verification code (via dashboard prompt)
5. Re-call r.login(mfa_code=code) → success, pickle saved
6. Rest of the day: pickle is valid, no re-auth needed
"""
import os
import sys
import pickle
import logging
from datetime import datetime, timedelta
from pathlib import Path

# CRITICAL: V1 path must go LAST (not at index 0) so V1's modules
# like config.py, indicators.py don't shadow V2's same-named modules.
# We add V1 path at the END so Python only finds V1's robin_stocks
# library, not V1's config/indicators/etc.
sys.path.insert(0, "/home/ralph/trader-v2")
sys.path.insert(0, "/home/ralph/trader-v2/scripts")
import config  # V2's config

# Now add V1 path at the END so robin_stocks is found but V1's
# config/indicators are NOT (they would shadow V2's versions).
sys.path.append("/home/ralph/robinhood-trainer")
import robin_stocks.robinhood as r
# IMPORTANT: import set_login_state from helper, not authentication,
# because authentication's version doesn't actually update LOGGED_IN
from robin_stocks.robinhood.helper import set_login_state
import robin_stocks.robinhood.helper as helper
from robin_stocks.robinhood.globals import LOGGED_IN
from rh_credentials import ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD

import config

log = logging.getLogger("v2.rh")

# Session storage
TOKEN_DIR = Path.home() / ".tokens"
PICKLE_PATH = TOKEN_DIR / "robinhood_v2.pickle"


class RobinhoodClient:
    """Wraps robin_stocks with safety rules, rate limiting, and 2FA handling."""

    def __init__(self):
        self.logged_in = False
        self.api_calls_this_minute = 0
        self.last_api_reset = datetime.now()
        self.requires_mfa = False  # True after a login challenge without mfa_code

    def _ensure_logged_in(self):
        """Auto-load from pickle if we have one and aren't logged in."""
        if self.logged_in:
            return True
        if not PICKLE_PATH.exists():
            return False
        try:
            with open(PICKLE_PATH, "rb") as f:
                pickle_data = pickle.load(f)
            access_token = pickle_data.get("access_token")
            token_type = pickle_data.get("token_type")
            if not access_token or not token_type:
                return False
            # CRITICAL: set_login_state MUST be called before any API call
            # because the library checks this global before every request
            set_login_state(True)
            r.update_session("Authorization", f"{token_type} {access_token}")
            profile = r.load_account_profile(info=None)
            if profile:
                self.logged_in = True
                log.info("Auto-logged in from shared pickle")
                return True
        except Exception as e:
            log.warning(f"Auto-login from pickle failed: {e}")
        return False

    def login(self, mfa_code=None):
        """
        Log in to Robinhood. Returns dict with:
            status: 'ok' | 'mfa_required' | 'error'
            detail: human-readable message
        """
        if self.logged_in:
            return {"status": "ok", "detail": "already_logged_in"}

        TOKEN_DIR.mkdir(parents=True, exist_ok=True)

        # Try pickle first
        if PICKLE_PATH.exists() and not mfa_code:
            try:
                with open(PICKLE_PATH, "rb") as f:
                    pickle_data = pickle.load(f)
                access_token = pickle_data.get("access_token")
                token_type = pickle_data.get("token_type")
                if access_token and token_type:
                    # CRITICAL: set_login_state MUST be called before API calls
                    set_login_state(True)
                    # Validate by hitting the API
                    r.update_session("Authorization", f"{token_type} {access_token}")
                    profile = r.load_account_profile(info=None)
                    if profile:
                        self.logged_in = True
                        set_login_state(True)
                        log.info("Loaded valid session from pickle")
                        return {"status": "ok", "detail": "from_pickle"}
            except Exception as e:
                log.warning(f"Pickle load failed: {e}")
                # Fall through to fresh login

        # Fresh login attempt
        try:
            if mfa_code:
                # Second attempt with MFA code
                result = r.login(
                    username=ROBINHOOD_USERNAME,
                    password=ROBINHOOD_PASSWORD,
                    expiresIn=86400,
                    mfa_code=mfa_code,
                    pickle_path=str(TOKEN_DIR),
                    pickle_name="_v2",
                )
                self.logged_in = True
                set_login_state(True)
                log.info("Logged in with MFA code")
                return {"status": "ok", "detail": "mfa_success"}
            else:
                # First attempt — may trigger MFA challenge
                try:
                    result = r.login(
                        username=ROBINHOOD_USERNAME,
                        password=ROBINHOOD_PASSWORD,
                        expiresIn=86400,
                        pickle_path=str(TOKEN_DIR),
                        pickle_name="_v2",
                    )
                    # Validate by actually hitting the API
                    try:
                        profile = r.load_account_profile(info=None)
                        if profile:
                            self.logged_in = True
                            set_login_state(True)
                            log.info("Logged in without MFA (already authenticated)")
                            return {"status": "ok", "detail": "no_mfa_needed"}
                        else:
                            raise Exception("login returned but profile is empty")
                    except Exception as ve:
                        log.error(f"Post-login validation failed: {ve}")
                        raise
                except Exception as e:
                    err_str = str(e).lower()
                    if "mfa" in err_str or "verification" in err_str or "challenge" in err_str or "401" in err_str:
                        self.requires_mfa = True
                        log.warning("MFA challenge required — push notification sent")
                        return {
                            "status": "mfa_required",
                            "detail": "approve_on_phone_then_provide_code",
                        }
                    raise
        except Exception as e:
            log.error(f"Login failed: {e}")
            return {"status": "error", "detail": str(e)}

    def _check_api_budget(self):
        """Track API calls and pause if over budget."""
        now = datetime.now()
        if (now - self.last_api_reset).seconds >= 60:
            self.api_calls_this_minute = 0
            self.last_api_reset = now
        self.api_calls_this_minute += 1

        budget = config.ROBINHOOD_API_RATE_LIMIT * config.API_BUDGET_PCT
        if self.api_calls_this_minute > budget:
            import time
            log.warning(f"API budget exceeded ({self.api_calls_this_minute}/{budget}), backing off")
            time.sleep(config.API_BACKOFF_SECONDS)

    # ---------------------------------------------------------------------
    # ACCOUNT
    # ---------------------------------------------------------------------
    def get_account_state(self):
        if not self._ensure_logged_in():
            return {}
        self._check_api_budget()
        try:
            profile = r.load_account_profile(info=None) or {}
            # Field mapping from actual Robinhood API response
            margin = profile.get("margin_balances", {}) or {}
            return {
                "equity": float(profile.get("portfolio_cash", 0) or 0),
                "buying_power": float(profile.get("buying_power", 0) or 0),
                "excess_margin": float(margin.get("day_trade_buying_power", 0) or 0),
                "excess_maintenance": float(margin.get("overnight_buying_power", 0) or 0),
                "cash_available_for_withdrawal": float(profile.get("cash_available_for_withdrawal", 0) or 0),
                "cash_held_for_orders": float(profile.get("cash_held_for_orders", 0) or 0),
                "unsettled_funds": float(profile.get("unsettled_funds", 0) or 0),
                "day_trade_buying_power": float(margin.get("day_trade_buying_power", 0) or 0),
                "overnight_buying_power": float(margin.get("overnight_buying_power", 0) or 0),
                "option_level": profile.get("option_level", ""),
            }
        except Exception as e:
            log.error(f"get_account_state failed: {e}")
            return {}

    # ---------------------------------------------------------------------
    # POSITIONS
    # ---------------------------------------------------------------------
    def get_open_option_positions(self):
        if not self._ensure_logged_in():
            return []
        self._check_api_budget()
        try:
            positions = r.get_aggregate_open_positions() or []
            enriched = []
            for p in positions:
                quantity = float(p.get("quantity", 0) or 0)
                if quantity == 0:
                    continue
                # Aggregate format puts instrument data in legs[0]
                legs = p.get("legs", [])
                if not legs:
                    continue
                leg = legs[0]
                instrument_id = leg.get("option_id", "")
                enriched.append({
                    "symbol": p.get("symbol") or leg.get("chain_symbol", ""),
                    "option_type": leg.get("option_type", ""),
                    "strike": float(leg.get("strike_price", 0) or 0),
                    "expiry": leg.get("expiration_date", ""),
                    "quantity": quantity,
                    "average_price": float(p.get("average_open_price", 0) or 0),
                    "instrument_id": instrument_id,
                    "position_id": p.get("id", ""),
                    "strategy": p.get("strategy", ""),
                })
            return enriched
        except Exception as e:
            log.error(f"get_open_option_positions failed: {e}")
            return []

    # ---------------------------------------------------------------------
    # ORDERS
    # ---------------------------------------------------------------------
    def get_pending_option_orders(self):
        if not self._ensure_logged_in():
            return []
        self._check_api_budget()
        try:
            orders = r.get_all_option_orders(info=None) or []
            pending = []
            for o in orders:
                state = o.get("state", "")
                if state in ("queued", "confirmed", "partially_filled", "open"):
                    pending.append({
                        "id": o.get("id", ""),
                        "state": state,
                        "price": float(o.get("price", 0) or 0),
                        "quantity": float(o.get("quantity", 0) or 0),
                        "side": o.get("side", ""),
                        "created_at": o.get("created_at", ""),
                    })
            return pending
        except Exception as e:
            log.error(f"get_pending_option_orders failed: {e}")
            return []

    def get_option_market_data(self, symbol, expiry, strike, option_type):
        if not self._ensure_logged_in():
            return None
        self._check_api_budget()
        try:
            # Use the proper robin_stocks API: get_option_market_data takes
            # (symbol, expirationDate, strikePrice, optionType)
            opt_data = r.get_option_market_data(
                symbol, expiry, strike, option_type, info=None
            )
            if not opt_data:
                return None
            # robin_stocks returns nested list [[{dict}]] — unwrap carefully
            if isinstance(opt_data, list):
                if opt_data and isinstance(opt_data[0], list):
                    opt_data = opt_data[0][0] if opt_data[0] else None
                elif opt_data:
                    opt_data = opt_data[0]
            if not opt_data or not isinstance(opt_data, dict):
                return None
            bid = float(opt_data.get("bid_price", 0) or 0)
            ask = float(opt_data.get("ask_price", 0) or 0)
            mark = (bid + ask) / 2 if bid > 0 and ask > 0 else float(opt_data.get("last_trade_price", 0) or 0)
            return {
                "instrument_id": opt_data.get("id", ""),
                "bid": bid,
                "ask": ask,
                "mark_price": mark,
                "volume": float(opt_data.get("volume", 0) or 0),
                "open_interest": float(opt_data.get("open_interest", 0) or 0),
                "delta": float(opt_data.get("delta", 0) or 0),
                "gamma": float(opt_data.get("gamma", 0) or 0),
                "theta": float(opt_data.get("theta", 0) or 0),
                "vega": float(opt_data.get("vega", 0) or 0),
                "implied_volatility": float(opt_data.get("implied_volatility", 0) or 0),
            }
        except Exception as e:
            log.error(f"get_option_market_data failed for {symbol}: {e}")
            return None

    def get_stock_quote(self, symbol):
        if not self._ensure_logged_in():
            return None
        self._check_api_budget()
        try:
            quote = r.get_quotes(symbol, info=None)
            if not quote:
                return None
            if isinstance(quote, list):
                quote = quote[0] if quote else None
            if not quote:
                return None
            return {
                "last": float(quote.get("last_trade_price", 0) or 0),
                "bid": float(quote.get("bid_price", 0) or 0),
                "ask": float(quote.get("ask_price", 0) or 0),
                "volume": float(quote.get("volume", 0) or 0),
                "previous_close": float(quote.get("previous_close", 0) or 0),
            }
        except Exception as e:
            log.error(f"get_stock_quote failed for {symbol}: {e}")
            return None


    def get_historicals(self, symbol, interval="5minute", span="week"):
        """
        2026-09-18: RH LOCKOUT — pull 1-min candles from the candle-vault (:8099, Alpaca SIP)
        and aggregate to the requested bar size (5m/10m/60m). RH shape out.
        """
        import requests as _rq
        from datetime import datetime as _dt, timedelta as _td
        agg = {"5minute": 5, "10minute": 10, "hour": 60}.get(interval, 5)
        days = {"week": 7, "day": 1}.get(span, 7)
        try:
            start = (_dt.utcnow() - _td(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
            r_ = _rq.get("http://localhost:8099/candles",
                         params={"symbol": symbol, "start": start, "limit": 20000},
                         timeout=15)
            bars = r_.json().get("bars", [])
            if not bars:
                return []
            out = []
            chunk = {}
            for b in bars:
                ts = b["t"]  # 'YYYY-MM-DDTHH:MM'
                base = ts[:16]
                dt = _dt.strptime(base, "%Y-%m-%dT%H:%M")
                slot = dt - _td(minutes=dt.minute % agg, seconds=dt.second)
                key = slot.strftime("%Y-%m-%dT%H:%M")
                if key not in chunk:
                    chunk[key] = {"begins_at": key + ":00Z", "open_price": b["o"],
                                  "high_price": b["h"], "low_price": b["l"],
                                  "close_price": b["c"], "volume": b["v"] or 0}
                else:
                    c = chunk[key]
                    c["high_price"] = max(c["high_price"], b["h"])
                    c["low_price"] = min(c["low_price"], b["l"])
                    c["close_price"] = b["c"]
                    c["volume"] += b["v"] or 0
            out = sorted(chunk.values(), key=lambda x: x["begins_at"])
            return out
        except Exception as e:
            log.error(f"vault get_historicals failed for {symbol}: {e}")
            return []

    def order_buy_option_limit(self, symbol, expiry, strike, option_type, quantity, limit_price, time_in_force="gtc"):
        self._check_api_budget()
        try:
            return r.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",
                symbol=symbol,
                expirationDate=expiry,
                strike=strike,
                optionType=option_type,
                quantity=quantity,
                price=limit_price,
                timeInForce=time_in_force,
            )
        except Exception as e:
            log.error(f"order_buy_option_limit failed: {e}")
            return None

    def order_sell_option_limit(self, symbol, expiry, strike, option_type, quantity, limit_price, time_in_force="gtc"):
        self._check_api_budget()
        try:
            return r.order_sell_option_limit(
                positionEffect="close",
                creditOrDebit="credit",
                symbol=symbol,
                expirationDate=expiry,
                strike=strike,
                optionType=option_type,
                quantity=quantity,
                price=limit_price,
                timeInForce=time_in_force,
            )
        except Exception as e:
            log.error(f"order_sell_option_limit failed: {e}")
            return None

    def get_option_order_full(self, order_id):
        """Full order detail incl. legs with resolved option specs (symbol,
        strike, expiry, type) — everything needed to cancel+re-place."""
        self._check_api_budget()
        try:
            o = r.get_option_order_info(order_id)
            if not o:
                return None
            legs = []
            for leg in o.get("legs", []):
                spec = {}
                try:
                    inst = helper.request_get(leg.get("option"))
                    if isinstance(inst, dict):
                        spec = {
                            "symbol": inst.get("chain_symbol"),
                            "strike": float(inst.get("strike_price", 0) or 0),
                            "expiry": inst.get("expiration_date"),
                            "option_type": inst.get("type"),
                        }
                except Exception as e:
                    log.warning(f"leg instrument resolve failed: {e}")
                legs.append({**spec, "side": leg.get("side"), "position_effect": leg.get("position_effect")})
            return {
                "id": o.get("id"),
                "state": o.get("derived_state") or o.get("state"),
                "price": float(o.get("price", 0) or 0),
                "quantity": float(o.get("quantity", 0) or 0),
                "time_in_force": o.get("time_in_force"),
                "created_at": o.get("created_at"),
                "cancelable": bool(o.get("cancel_url")),
                "legs": legs,
            }
        except Exception as e:
            log.error(f"get_option_order_full failed for {order_id}: {e}")
            return None

    def replace_option_order(self, order_id, price=None, quantity=None, time_in_force=None):
        """Replace a live order: cancel it, re-place same legs with new
        price/quantity/TIF. Robinhood has no native modify — this is the
        standard cancel+re-place. Returns (ok, new_order_or_error)."""
        detail = self.get_option_order_full(order_id)
        if not detail:
            return False, {"error": "order not found"}
        if not detail["cancelable"]:
            return False, {"error": f"order not cancelable (state={detail['state']})"}
        leg = detail["legs"][0] if detail["legs"] else None
        if not leg or not leg.get("symbol"):
            return False, {"error": "could not resolve order legs"}
        new_price = price if price is not None else detail["price"]
        new_qty = int(quantity if quantity is not None else detail["quantity"])
        new_tif = time_in_force if time_in_force is not None else (detail["time_in_force"] or "gtc")
        if new_tif not in ("gtc", "gfd", "ioc"):
            return False, {"error": f"invalid time_in_force: {new_tif}"}
        if new_price <= 0 or new_qty <= 0:
            return False, {"error": "price and quantity must be positive"}

        # 1. Cancel the old order first
        cancel_ok = self.cancel_option_order(order_id)
        if not cancel_ok:
            return False, {"error": "cancel failed — old order still live, nothing re-placed"}

        # 2. Re-place with same legs at new parameters
        log.info(f"[REPLACE] cancel {order_id} -> re-place {leg['symbol']} "
                 f"{leg['strike']} {leg['option_type']} x{new_qty} @ ${new_price} tif={new_tif}")
        if leg["side"] == "buy":
            new_order = self.order_buy_option_limit(
                symbol=leg["symbol"], expiry=leg["expiry"], strike=leg["strike"],
                option_type=leg["option_type"], quantity=new_qty,
                limit_price=new_price, time_in_force=new_tif,
            )
        else:
            new_order = self.order_sell_option_limit(
                symbol=leg["symbol"], expiry=leg["expiry"], strike=leg["strike"],
                option_type=leg["option_type"], quantity=new_qty,
                limit_price=new_price, time_in_force=new_tif,
            )
        if not new_order or not isinstance(new_order, dict) or not new_order.get("id"):
            return False, {"error": f"cancel OK but re-place FAILED — place manually. resp={str(new_order)[:150]}"}
        return True, {"new_order_id": new_order["id"], "price": new_price,
                      "quantity": new_qty, "time_in_force": new_tif}

    def review_option_order(self, symbol, quantity, option_type, strike, expiry, side="buy", price=None):
        """Simulate an option order WITHOUT placing it.

        Returns the order details with per-leg market data and collateral needed.
        If this fails or shows bad pricing, we skip the trade.

        Args:
            symbol: e.g. "NVDA"
            quantity: number of contracts
            option_type: "call" or "put"
            strike: strike price
            expiry: e.g. "2026-08-15"
            side: "buy" or "sell"
            price: limit price (optional, uses mid if not given)

        Returns:
            dict with order preview or None if invalid
        """
        self._check_api_budget()
        try:
            from datetime import datetime
            if isinstance(expiry, str):
                expiry_date_str = expiry
            else:
                expiry_date_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)

            # Get chain for the symbol
            chain = r.get_chains(symbol)
            if not chain:
                return {"valid": False, "error": f"No options chain for {symbol}"}

            # Get options - filter by expiry and type
            try:
                options = r.find_options_by_expiration(
                    symbol,
                    expirationDate=expiry_date_str,
                    optionType=option_type,
                )
            except Exception:
                options = []

            # Match by strike
            matching = None
            for opt in options or []:
                try:
                    if abs(float(opt.get("strike_price", 0)) - float(strike)) < 0.001:
                        matching = opt
                        break
                except Exception:
                    continue

            if not matching:
                return {"valid": False, "error": f"No option found for {symbol} {option_type} ${strike} exp {expiry_date_str}"}

            # Get current market data for the option
            market = self.get_option_market_data(symbol, expiry_date_str, float(strike), option_type)
            if not market:
                return {"valid": False, "error": "Could not fetch market data"}

            # Normalize market data shape (wrapper returns bid/ask, raw returns bid_price/ask_price)
            bid = float(market.get("bid", market.get("bid_price", 0)) or 0)
            ask = float(market.get("ask", market.get("ask_price", 0)) or 0)
            mid = (bid + ask) / 2 if bid and ask else 0

            if price is None:
                price = mid if mid else (ask or bid)

            # Sanity checks
            spread_pct = ((ask - bid) / ask * 100) if ask > 0 else 100
            warnings = []
            if spread_pct > 20:
                warnings.append(f"wide spread ({spread_pct:.0f}%)")
            if quantity < 1:
                warnings.append("quantity < 1")

            return {
                "valid": True,
                "symbol": symbol,
                "option_type": option_type,
                "strike": strike,
                "expiry": expiry_date_str,
                "quantity": quantity,
                "side": side,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "limit_price": price,
                "spread": ask - bid if bid and ask else 0,
                "spread_pct": spread_pct,
                "estimated_cost": price * quantity * 100,  # Each contract = 100 shares
                "option_id": matching.get("id", ""),
                "warnings": warnings,
                "market_data": market,
            }
        except Exception as e:
            log.error(f"review_option_order failed: {e}")
            return {"valid": False, "error": str(e)}

    def get_market_status(self, market="XNYS"):
        """Check if a market is currently open.

        Returns:
            dict with:
                is_open: bool
                today_open: ISO datetime or None
                today_close: ISO datetime or None
                next_open: ISO datetime or None
        """
        self._check_api_budget()
        try:
            from datetime import datetime, timezone
            today = datetime.now().strftime("%Y-%m-%d")
            hours = r.get_market_hours(market, today)
            if not hours:
                return {"is_open": False, "error": "No market hours data"}

            # Get next open hours
            try:
                next_hours = r.get_market_next_open_hours(market)
            except Exception:
                next_hours = None

            # Today's session
            opens_at = hours.get("opens_at")
            closes_at = hours.get("closes_at")

            is_open = bool(hours.get("is_open", False))
            if not is_open and opens_at and closes_at:
                # Double-check using current time
                try:
                    now = datetime.now(timezone.utc)
                    o = datetime.fromisoformat(opens_at.replace("Z", "+00:00"))
                    c = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
                    is_open = o <= now < c
                except Exception:
                    pass

            return {
                "is_open": is_open,
                "today_date": hours.get("date", today),
                "today_open": opens_at,
                "today_close": closes_at,
                "next_open": next_hours.get("opens_at") if next_hours else None,
                "next_close": next_hours.get("closes_at") if next_hours else None,
            }
        except Exception as e:
            log.error(f"get_market_status failed: {e}")
            return {"is_open": False, "error": str(e)}

    def has_upcoming_earnings(self, symbol, within_days=3):
        """Check if symbol has earnings within N days.

        Returns:
            (has_earnings, days_until) tuple
        """
        self._check_api_budget()
        try:
            earnings = r.get_earnings(symbol)
            if not earnings:
                return (False, None)

            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(days=within_days)

            for e in earnings:
                # Earnings are in the future only
                report_date_str = e.get("report", {}).get("date", "") if isinstance(e.get("report"), dict) else ""
                if not report_date_str:
                    continue
                # Parse date
                try:
                    if "T" in report_date_str:
                        report_date = datetime.fromisoformat(report_date_str.replace("Z", "+00:00"))
                    else:
                        report_date = datetime.strptime(report_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

                    if now <= report_date <= cutoff:
                        days_until = (report_date - now).days
                        return (True, days_until)
                except Exception:
                    continue
            return (False, None)
        except Exception as e:
            log.error(f"has_upcoming_earnings failed: {e}")
            return (False, None)

    def cancel_option_order(self, order_id):
        self._check_api_budget()
        try:
            return r.cancel_option_order(order_id)
        except Exception as e:
            log.error(f"cancel_option_order failed for {order_id}: {e}")
            return False

    def get_order_status(self, order_id):
        """Get order status with full fill details.

        Returns derived_state (more accurate than state), processed_quantity,
        actual fill price from executions, and pending_quantity for partial
        fill detection.
        """
        self._check_api_budget()
        try:
            order = r.get_option_order_info(order_id)
            if not order:
                return None

            # Use derived_state — more accurate than state field
            # (state may say "confirmed" while derived_state shows "filled")
            state = order.get("derived_state") or order.get("state", "")

            # Process all legs to get total fill quantity and average price
            legs = order.get("legs", [])
            total_quantity = 0
            total_notional = 0.0
            total_pending = 0.0
            total_ordered = 0.0

            for leg in legs:
                # processed_quantity is the filled amount
                leg_qty = float(leg.get("processed_quantity", 0) or 0)
                total_quantity += leg_qty

                # pending_quantity is the unfilled amount (for partial fills)
                leg_pending = float(leg.get("pending_quantity", 0) or 0)
                total_pending += leg_pending

                # Total ordered = filled + pending
                leg_ordered = leg_qty + leg_pending
                total_ordered += leg_ordered

                # Use executions array for actual fill prices
                executions = leg.get("executions", [])
                for exec_data in executions:
                    qty = float(exec_data.get("quantity", 0) or 0)
                    price = float(exec_data.get("price", 0) or 0)
                    total_notional += qty * price

            # Average price across all fills
            average_price = (total_notional / total_quantity) if total_quantity > 0 else 0.0

            return {
                "id": order.get("id", ""),
                "state": state,
                "filled_quantity": total_quantity,
                "pending_quantity": total_pending,
                "ordered_quantity": total_ordered,
                "average_price": average_price,
                "net_amount": float(order.get("net_amount", 0) or 0),
                "executions_count": sum(len(leg.get("executions", [])) for leg in legs),
            }
        except Exception as e:
            log.error(f"get_order_status failed for {order_id}: {e}")
            return None


# Singleton
_client = None


def get_client():
    global _client
    if _client is None:
        _client = RobinhoodClient()
    return _client
