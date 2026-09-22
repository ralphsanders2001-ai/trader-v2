"""
Trader V2 dashboard — minimal Flask app for live trading.

For tonight's demo, just three pages:
- /  — open positions with SELL NOW buttons + system status
- /settings — edit config values (persist to config.py)
- /auth — provide MFA code if login is waiting

Port 8091 to avoid conflict with V1 on 8090.
"""
import os
import sys
import json
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for

sys.path.insert(0, "/home/ralph/trader-v2")
sys.path.insert(0, "/home/ralph/trader-v2/scripts")

import config
import config_loader
import json as json_module
import glob
BACKTEST_RESULTS_DIR = Path("/home/ralph/trader-v2/backtest_results")

from database import get_connection
from robinhood_client import get_client
import executor

app = Flask(__name__)
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
app.template_folder = str(TEMPLATE_DIR)
TEMPLATE_DIR.mkdir(exist_ok=True)


@app.route("/")
def index():
    conn = get_connection()
    positions = conn.execute("SELECT * FROM positions").fetchall()
    closed_today = conn.execute("""
        SELECT * FROM trades
        WHERE timestamp_close IS NOT NULL
        AND date(timestamp_close) = date('now')
        ORDER BY timestamp_close DESC LIMIT 20
    """).fetchall()
    # Recent signals: candidates, fills, and executor rejections (last 4h)
    recent_signals = conn.execute("""
        SELECT timestamp, symbol, direction, status, rejection_reason, score,
               spread_pct, volume_ratio, rsi_value
        FROM signals
        WHERE created_at >= datetime('now', '-4 hours')
          AND status IN ('candidate', 'filled', 'rejected')
          AND (rejection_reason IS NULL
               OR rejection_reason NOT IN ('no_signal', 'no_options', 'no_contract',
                                           'blocked_for_day', 'symbol_disabled'))
        ORDER BY id DESC LIMIT 25
    """).fetchall()
    circuit = conn.execute("SELECT * FROM circuit_breaker WHERE id=1").fetchone()
    conn.close()

    client = get_client()
    account = client.get_account_state()

    # Compute current P&L for each position
    pos_data = []
    for pos in positions:
        market = client.get_option_market_data(
            pos["symbol"], pos["option_expiry"], pos["option_strike"], pos["option_type"]
        )
        bid = market["bid"] if market else 0
        ask = market["ask"] if market else 0
        pnl_pct = ((bid - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] else 0
        pnl_dollar = (bid - pos["entry_price"]) * pos["quantity"] * 100
        pos_data.append({
            **dict(pos),
            "bid": bid,
            "ask": ask,
            "pnl_pct": pnl_pct,
            "pnl_dollar": pnl_dollar,
        })

    return render_template("index.html",
                           positions=pos_data,
                           closed=closed_today,
                           recent_signals=recent_signals,
                           circuit=circuit,
                           account=account)


@app.route("/api/pending-orders")
def api_pending_orders():
    """Fetch all live (cancelable) option orders with full leg detail."""
    client = get_client()
    try:
        import robinhood_client as rc
        all_orders = rc.r.get_all_option_orders(info=None) or []
        pending = []
        for o in all_orders:
            if not o.get("cancel_url"):
                continue  # only live orders can be cancelled/replaced
            state = o.get("derived_state") or o.get("state", "")
            leg = (o.get("legs") or [{}])[0]
            spec = {}
            try:
                inst = rc.helper.request_get(leg.get("option"))
                if isinstance(inst, dict):
                    spec = {
                        "symbol": inst.get("chain_symbol", ""),
                        "strike": float(inst.get("strike_price", 0) or 0),
                        "expiry": inst.get("expiration_date", ""),
                        "option_type": inst.get("type", ""),
                    }
            except Exception:
                pass
            pending.append({
                "id": o["id"],
                "state": state,
                "time_in_force": o.get("time_in_force", ""),
                "side": leg.get("side", ""),
                "price": float(o.get("price", 0) or 0),
                "quantity": float(o.get("quantity", 0) or 0),
                "filled_quantity": float(o.get("filled_quantity", 0) or 0),
                "created_at": o.get("created_at", ""),
                **spec,
            })
        return jsonify({"orders": pending, "count": len(pending)})
    except Exception as e:
        return jsonify({"error": str(e), "orders": [], "count": 0}), 500


@app.route("/api/orders/cancel/<order_id>", methods=["POST"])
def api_cancel_order(order_id):
    """Cancel a pending order on Robinhood."""
    client = get_client()
    try:
        result = client.cancel_option_order(order_id)
        return jsonify({"status": "cancelled", "order_id": order_id, "result": result})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/orders/replace/<order_id>", methods=["POST"])
def api_replace_order(order_id):
    """Replace a live order: cancel + re-place with new price/qty/time-in-force."""
    client = get_client()
    data = request.json or {}
    try:
        price = float(data["price"]) if data.get("price") not in (None, "") else None
        qty = int(float(data["quantity"])) if data.get("quantity") not in (None, "") else None
        tif = data.get("time_in_force") or None
        ok, result = client.replace_option_order(order_id, price=price, quantity=qty, time_in_force=tif)
        return jsonify({"success": ok, "order_id": order_id, **result}), (200 if ok else 400)
    except ValueError:
        return jsonify({"success": False, "error": "invalid price/quantity"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_id>", methods=["GET"])
def api_order_detail(order_id):
    """Full detail for one order (legs, tif, cancelable state)."""
    client = get_client()
    detail = client.get_option_order_full(order_id)
    if not detail:
        return jsonify({"error": "not found"}), 404
    return jsonify(detail)


@app.route("/api/pause-trading", methods=["POST"])
def api_pause_trading():
    """Pause trading by tripping the circuit breaker.

    The daemon checks circuit_breaker.tripped before each trade.
    When tripped=1, it skips new entries but still manages open positions.
    """
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
        UPDATE circuit_breaker
        SET tripped=1, tripped_at=datetime('now'), reason='manual_pause',
            trading_day=?, last_updated=datetime('now')
        WHERE id=1
    """, (today,))
    if conn.total_changes == 0:
        conn.execute("""
            INSERT INTO circuit_breaker (id, trading_day, tripped, tripped_at, reason, realized_pnl)
            VALUES (1, ?, 1, datetime('now'), 'manual_pause', 0)
        """, (today,))
    conn.commit()
    conn.close()
    return jsonify({"status": "paused", "trading_day": today})


@app.route("/api/resume-trading", methods=["POST"])
def api_resume_trading():
    """Resume trading by clearing the circuit breaker."""
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
        UPDATE circuit_breaker SET tripped=0, reason='manual_resume'
        WHERE trading_day = ?
    """, (today,))
    conn.commit()
    conn.close()
    return jsonify({"status": "resumed", "trading_day": today})


@app.route("/api/trading-status")
def api_trading_status():
    """Check if trading is currently paused."""
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("""
        SELECT tripped, reason, tripped_at, trading_day FROM circuit_breaker
        WHERE id=1
    """).fetchone()
    conn.close()
    if row:
        return jsonify({
            "trading_day": row[3] or today,
            "paused": bool(row[0]),
            "reason": row[1],
            "tripped_at": row[2],
        })
    return jsonify({"trading_day": today, "paused": False, "reason": None, "tripped_at": None})


@app.route("/api/sell/<int:position_id>", methods=["POST"])
def api_sell(position_id):
    """SELL NOW button — immediately close a position."""
    reason = request.json.get("reason", "manual") if request.json else "manual"
    success = executor.execute_sell(position_id, reason=reason)
    return jsonify({"success": success, "position_id": position_id})


@app.route("/api/hold/<int:position_id>", methods=["POST"])
def api_hold(position_id):
    """HOLD button — mark position as held (overrides ALL auto-sell rules)."""
    conn = get_connection()
    conn.execute("UPDATE positions SET on_hold = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (position_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "position_id": position_id, "on_hold": True})


@app.route("/api/unhold/<int:position_id>", methods=["POST"])
def api_unhold(position_id):
    """UNHOLD button — release hold so normal exit rules apply again."""
    conn = get_connection()
    conn.execute("UPDATE positions SET on_hold = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (position_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "position_id": position_id, "on_hold": False})


# Module-level so /api/settings/update can validate types against the same spec
def _settings_groups():
    # Group config values for the form
    return {
        "Poll Tiers (seconds)": {
            "POLL_TIERS.high_velocity": {
                "value": config.POLL_TIERS["high_velocity"],
                "type": "int", "label": "High velocity (5-15s)"
            },
            "POLL_TIERS.standard": {
                "value": config.POLL_TIERS["standard"],
                "type": "int", "label": "Standard (10-30s)"
            },
            "POLL_TIERS.low_volatility": {
                "value": config.POLL_TIERS["low_volatility"],
                "type": "int", "label": "Low volatility (15-60s)"
            },
            "SCAN_INTERVAL": {
                "value": config.SCAN_INTERVAL,
                "type": "int", "label": "Entry scan interval (seconds)"
            },
            "MAX_ACTIVE_SYMBOLS": {
                "value": getattr(config, "MAX_ACTIVE_SYMBOLS", 12),
                "type": "int", "label": "Max active (enabled) symbols"
            },
        },
        "Order Pricing": {
            "LIMIT_PRICE_OFFSET": {"value": config.LIMIT_PRICE_OFFSET, "type": "float", "label": "Limit offset $ (buy above ask / sell below bid)"},
            "BUY_LIMIT_OFFSET_PCT": {"value": getattr(config, "BUY_LIMIT_OFFSET_PCT", 0.0), "type": "float", "label": "Buy % below ask (0 = off, e.g. 2 = 2% under ask)"},
        },
        "Loss Caps": {
            "LOSS_CAP_PCT": {"value": config.LOSS_CAP_PCT, "type": "float", "label": "Max loss % of premium per trade (GOVERNS exits)"},
            "LOSS_CAP_DOLLAR": {"value": config.LOSS_CAP_DOLLAR, "type": "float", "label": "Max loss $ per trade (GOVERNS exits; smaller of $ or % applies)"},
            "MAX_DAILY_LOSS": {"value": config.MAX_DAILY_LOSS, "type": "float", "label": "Daily circuit breaker ($)"},
        },
        "Profit Targets": {
            "TAKE_PROFIT_DOLLAR": {
                "value": getattr(config, "TAKE_PROFIT_DOLLAR", 10.0),
                "type": "float", "label": "Take profit $ per trade (GOVERNS exits)"
            },
            "MAX_PROFIT_DOLLAR": {
                "value": getattr(config, "MAX_PROFIT_DOLLAR", 50.0),
                "type": "float", "label": "Max profit $ per trade (GOVERNS exits)"
            },
            "PROFIT_TARGET_PCT": {
                "value": getattr(config, "PROFIT_TARGET_PCT", 0.10),
                "type": "float", "label": "Sell-off on every entry (+% over entry)"
            },
            "TIER_PROFIT_TARGETS.high_velocity": {
                "value": config.TIER_PROFIT_TARGETS["high_velocity"],
                "type": "float", "label": "High velocity (volatile)"
            },
            "TIER_PROFIT_TARGETS.standard": {
                "value": config.TIER_PROFIT_TARGETS["standard"],
                "type": "float", "label": "Standard"
            },
            "TIER_PROFIT_TARGETS.low_volatility": {
                "value": config.TIER_PROFIT_TARGETS["low_volatility"],
                "type": "float", "label": "Low volatility"
            },
        },
        "Position Limits": {
            "MAX_HOLD_MINUTES": {"value": config.MAX_HOLD_MINUTES, "type": "int", "label": "Max hold time (min)"},
            "MAX_BUYS_PER_DAY": {"value": config.MAX_BUYS_PER_DAY, "type": "int", "label": "Max buys per day"},
            "POSITION_SIZE_CONTRACTS": {"value": config.POSITION_SIZE_CONTRACTS, "type": "int", "label": "Base position size"},
        },
        "Cooldown": {
            "COOLDOWN_FIRST_LOSS_MINUTES": {"value": config.COOLDOWN_FIRST_LOSS_MINUTES, "type": "int", "label": "After 1st loss (min)"},
            "COOLDOWN_SECOND_LOSS_MINUTES": {"value": config.COOLDOWN_SECOND_LOSS_MINUTES, "type": "int", "label": "After 2nd loss (min)"},
            "SYMBOL_COOLDOWN_WIN_SEC": {"value": getattr(config, "SYMBOL_COOLDOWN_WIN_SEC", 900), "type": "int", "label": "Re-entry cooldown after WIN (sec)"},
            "SYMBOL_COOLDOWN_LOSS_SEC": {"value": getattr(config, "SYMBOL_COOLDOWN_LOSS_SEC", 900), "type": "int", "label": "Re-entry cooldown after LOSS (sec)"},
        },
        "Signal Quality": {
            "MIN_SCORE_THRESHOLD": {"value": getattr(config, "MIN_SCORE_THRESHOLD", 50), "type": "int", "label": "Min score to trade (floor on adaptive gate)"},
            "USE_ML_GATE": {"value": getattr(config, "USE_ML_GATE", True), "type": "bool", "label": "ML gate ON (XGBoost entry filter)"},
            "USE_LLM_GATE": {"value": getattr(config, "USE_LLM_GATE", True), "type": "bool", "label": "LLM second-opinion ON (borderline ML calls)"},
            "DAILY_LOSS_PAUSE": {"value": getattr(config, "DAILY_LOSS_PAUSE", -200), "type": "int", "label": "Pause entries at daily loss (negative $; 0 = disabled)"},
        },
        "Market Hours": {
            "NO_TRADE_START_HHMM": {"value": config.NO_TRADE_START_HHMM, "type": "str", "label": "No-trade start"},
            "NO_TRADE_END_HHMM": {"value": config.NO_TRADE_END_HHMM, "type": "str", "label": "No-trade end"},
            "ENABLE_NO_TRADE_WINDOW": {"value": config.ENABLE_NO_TRADE_WINDOW, "type": "bool", "label": "Enable no-trade window"},
        },
        "Watchlist": {
            "FORCED_TURBULENT_SYMBOLS": {
                "value": ",".join(config.FORCED_TURBULENT_SYMBOLS),
                "type": "list", "label": "Forced volatile symbols (comma-separated)"
            },
        },
    }

@app.route("/settings")
def settings():
    """Show all configurable settings as a form."""
    groups = _settings_groups()
    return render_template("settings.html", groups=groups)


@app.route("/api/settings/update", methods=["POST"])
def api_settings_update():
    """Update a config value and persist to disk."""
    data = request.json
    key = data.get("key")
    value = data.get("value")
    if not key:
        return jsonify({"success": False, "error": "no key"}), 400
    try:
        # Validate numeric types BEFORE writing — a stray char in an int/float field
        # must return a clean 400, not a 500 (and never a half-written config).
        groups = _settings_groups()
        expected_type = None
        for _g in groups.values():
            for _k, _spec in _g.items():
                if _k == key:
                    expected_type = _spec.get("type")
                    break
        if expected_type in ("int", "float"):
            try:
                float(value)  # rejects 'abc', '', None-as-string
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": f"'{key}' needs a {'whole number' if expected_type=='int' else 'number'} — got '{value}'"}), 400
        old, new = config_loader.set_value(key, value)
        # Log the change
        conn = get_connection()
        conn.execute("""
            INSERT INTO config_changes (config_key, old_value, new_value, source)
            VALUES (?, ?, ?, 'dashboard')
        """, (key, str(old), str(new)))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "key": key, "old": str(old), "new": str(new)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# --- Daily state: cooldown status / resets / balance ---------------------------

@app.route("/api/daily-state")
def api_daily_state():
    """Per-symbol cooldown/blocked status + today's circuit-breaker P&L."""
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT symbol, losses_today, wins_today, consecutive_losses,
               cooldown_until, blocked_for_day, realized_pnl, last_trade_time
        FROM symbol_daily_state WHERE trading_day=?
        ORDER BY symbol
    """, (today,)).fetchall()
    cb = conn.execute(
        "SELECT trading_day, realized_pnl, tripped, reason FROM circuit_breaker WHERE trading_day=?",
        (today,)
    ).fetchone()
    conn.close()
    now = datetime.now().astimezone()
    symbols = []
    for r in rows:
        in_cooldown = False
        remaining_min = 0
        if r["cooldown_until"]:
            try:
                until = datetime.fromisoformat(r["cooldown_until"])
                secs = (until - now.replace(tzinfo=until.tzinfo)).total_seconds()
                if secs > 0:
                    in_cooldown = True
                    remaining_min = round(secs / 60)
            except Exception:
                pass
        symbols.append({
            "symbol": r["symbol"],
            "losses_today": r["losses_today"],
            "wins_today": r["wins_today"],
            "blocked_for_day": bool(r["blocked_for_day"]),
            "in_cooldown": in_cooldown or bool(r["blocked_for_day"]),
            "cooldown_remaining_min": remaining_min,
            "cooldown_until": r["cooldown_until"],
            "realized_pnl": round(r["realized_pnl"], 2),
        })
    return jsonify({
        "trading_day": today,
        "symbols": symbols,
        "circuit_breaker": {
            "realized_pnl": round(cb["realized_pnl"], 2) if cb else 0.0,
            "tripped": bool(cb["tripped"]) if cb else False,
            "reason": cb["reason"] if cb else None,
        } if cb else None,
    })


@app.route("/api/daily-state/reset", methods=["POST"])
def api_daily_state_reset():
    """Reset loss counters / cooldowns / blocked flags. Optional: symbols list."""
    data = request.json or {}
    symbols = data.get("symbols")  # None => all symbols
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    if symbols:
        qmarks = ",".join("?" for _ in symbols)
        n = conn.execute(f"""
            UPDATE symbol_daily_state
            SET cooldown_until=NULL, blocked_for_day=0,
                losses_today=0, consecutive_losses=0
            WHERE trading_day=? AND symbol IN ({qmarks})
        """, (today, *[s.upper().strip() for s in symbols])).rowcount
    else:
        n = conn.execute("""
            UPDATE symbol_daily_state
            SET cooldown_until=NULL, blocked_for_day=0,
                losses_today=0, consecutive_losses=0
            WHERE trading_day=?
        """, (today,)).rowcount
    conn.commit()
    conn.close()
    return jsonify({"success": True, "rows_reset": n, "day": today})


@app.route("/api/circuit-breaker/reset", methods=["POST"])
def api_circuit_breaker_reset():
    """Reset today's daily loss circuit breaker (realized_pnl -> 0, untrip)."""
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
        INSERT INTO circuit_breaker (id, trading_day, realized_pnl, tripped, tripped_at, reason, last_updated)
        VALUES (1, ?, 0.0, 0, NULL, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET
            realized_pnl=0.0, tripped=0, tripped_at=NULL, reason=NULL, last_updated=excluded.last_updated
    """, (today, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "day": today, "realized_pnl": 0.0, "tripped": False})


@app.route("/api/capital")
def api_capital():
    """Capital governor state: $190 base + running realized P&L, reserved/available."""
    try:
        import capital as capital_mod
        capital_mod.init_capital_table()
        st = capital_mod.get_state()
        realized = capital_mod.realized_pnl()
        reserved = capital_mod.reserved_cash()
        bal = st["starting_balance"] + realized
        return jsonify({
            "starting_balance": round(st["starting_balance"], 2),
            "realized_pnl": round(realized, 2),
            "balance": round(bal, 2),
            "reserved": round(reserved, 2),
            "available": round(bal - reserved, 2),
            "trade_id_marker": st["trade_id_marker"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/capital/set", methods=["POST"])
def api_capital_set():
    """Set the capital governor starting balance (e.g. $190)."""
    data = request.json or {}
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "invalid number"}), 400
    import capital as capital_mod
    old = capital_mod.get_state()["starting_balance"]
    capital_mod.set_starting_balance(value)
    conn = get_connection()
    conn.execute("""
        INSERT INTO config_changes (config_key, old_value, new_value, source)
        VALUES ('CAPITAL_STARTING_BALANCE', ?, ?, 'dashboard')
    """, (str(old), str(value)))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "old": old, "new": value})


@app.route("/api/paper-pending")
def api_paper_pending():
    """Queued paper buys (patient bids below the current bid, waiting for the
    ask to drop). Shown on the dashboard so queued entries are visible."""
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
    rows = conn.execute(
        "SELECT id, created_at, symbol, option_type, option_strike, option_expiry, limit_price "
        "FROM paper_pending_buys WHERE active=1 ORDER BY id"
    ).fetchall()
    conn.close()
    # Realtime pricing per queued bid (2026-09-21, Ralph): live bid/ask next to
    # the resting limit so you can watch the price approach the fill.
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo as _ZI
    _et = _ZI("America/New_York")
    _client = get_client()
    pending = []
    for r in rows:
        d = dict(r)
        try:
            m = _client.get_option_market_data(
                d["symbol"], d["option_expiry"], d["option_strike"], d["option_type"]
            )
            d["live_bid"] = m.get("bid") if m else None
            d["live_ask"] = m.get("ask") if m else None
            d["will_fill_now"] = bool(m and m.get("ask", 0) > 0 and m["ask"] <= d["limit_price"])
        except Exception:
            d["live_bid"] = None
            d["live_ask"] = None
            d["will_fill_now"] = False
        # Queue time: stored UTC -> display ET
        try:
            _u = _dt.strptime(d["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
            d["queued_et"] = _u.astimezone(_et).strftime("%H:%M:%S ET")
        except Exception:
            d["queued_et"] = d["created_at"]
        pending.append(d)
    return jsonify({"pending": pending, "count": len(pending)})


@app.route("/api/balance", methods=["POST"])
def api_balance_set():
    """Set the trader's daily starting balance (persisted to config.py)."""
    data = request.json or {}
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "invalid number"}), 400
    old, new = config_loader.set_value("DAILY_STARTING_BALANCE", value)
    conn = get_connection()
    conn.execute("""
        INSERT INTO config_changes (config_key, old_value, new_value, source)
        VALUES ('DAILY_STARTING_BALANCE', ?, ?, 'dashboard')
    """, (str(old), str(new)))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "key": "DAILY_STARTING_BALANCE", "old": str(old), "new": str(new)})


# --- Per-symbol settings overrides (2026-09-16) -------------------------------

import threading as _threading
_SYMSET_LOCK = _threading.Lock()

SYMBOL_SETTING_KEYS = {
    "trading_hours":   "str",   "buy_pct_offset": "float", "limit_offset": "float",
    "exit_pct": "float", "sell_off_pct": "float", "max_loss_pct": "float",
    "max_loss_dollar": "float", "take_profit": "float", "max_profit": "float",
    "hold_time_min":   "int",
}


@app.route("/api/symbol-settings")
def api_symbol_settings():
    """Per-symbol overrides + the globals they override (for the settings UI)."""
    try:
        config_loader.reload_if_changed()  # pick up hand edits + cross-process writes
    except Exception:
        pass
    import symbol_settings as ss
    overrides = ss.all_overrides()
    globals_map = {
        "trading_hours": None,
        "buy_pct_offset": getattr(config, "BUY_LIMIT_OFFSET_PCT", 0.0),
        "limit_offset": getattr(config, "LIMIT_PRICE_OFFSET", 0.0),
        "exit_pct": getattr(config, "PROFIT_TARGET_PCT", 0.10),
        "max_loss_pct": getattr(config, "LOSS_CAP_PCT", 0.25),
        "max_loss_dollar": getattr(config, "LOSS_CAP_DOLLAR", 40.0),
        "take_profit": getattr(config, "TAKE_PROFIT_DOLLAR", 10.0),
        "max_profit": getattr(config, "MAX_PROFIT_DOLLAR", 50.0),
        "hold_time_min": None,
    }
    return jsonify({"overrides": overrides, "globals": globals_map,
                    "keys": SYMBOL_SETTING_KEYS})


@app.route("/api/symbol-settings/set", methods=["POST"])
def api_symbol_settings_set():
    """Set one key for one symbol. body: {symbol, key, value} (value null/'' = clear)."""
    data = request.json or {}
    sym = (data.get("symbol") or "").upper().strip()
    key = data.get("key") or ""
    value = data.get("value")
    if not sym or key not in SYMBOL_SETTING_KEYS:
        return jsonify({"success": False, "error": "bad symbol or key"}), 400
    ktype = SYMBOL_SETTING_KEYS[key]
    if value in (None, "", "null"):
        value = None  # clear
    elif ktype == "float":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": key + " needs a number"}), 400
    elif ktype == "int":
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": key + " needs a whole number"}), 400
    elif ktype == "str" and key == "trading_hours" and value is not None:
        import re as _re
        if not _re.match(r'^\d{1,2}:\d{2}-\d{1,2}:\d{2}$', str(value).strip()):
            return jsonify({"success": False, "error": "trading_hours must be HH:MM-HH:MM (ET)"}), 400
    import symbol_settings as _ss
    with _SYMSET_LOCK:
        try:
            config_loader.reload_if_changed()
        except Exception:
            pass
        table = dict(_ss.all_overrides())
        entry = dict(table.get(sym, {}))
        if value is None:
            entry.pop(key, None)
            if not entry:
                table.pop(sym, None)
            else:
                table[sym] = entry
        else:
            entry[key] = value
            table[sym] = entry
        old, new = config_loader.set_value("SYMBOL_SETTINGS", table)
    conn = get_connection()
    conn.execute("INSERT INTO config_changes (config_key, old_value, new_value, source) VALUES ('SYMBOL_SETTINGS', ?, ?, 'dashboard')", (str(old), str(new)))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "symbol": sym, "key": key, "value": value})


@app.route("/api/symbol-settings/clear", methods=["POST"])
def api_symbol_settings_clear():
    """Clear ALL overrides for one symbol. body: {symbol}"""
    data = request.json or {}
    sym = (data.get("symbol") or "").upper().strip()
    if not sym:
        return jsonify({"success": False, "error": "no symbol"}), 400
    import symbol_settings as _ss
    try:
        config_loader.reload_if_changed()
    except Exception:
        pass
    table = dict(_ss.all_overrides())
    old = table.get(sym)
    table.pop(sym, None)
    old2, new2 = config_loader.set_value("SYMBOL_SETTINGS", table)
    conn = get_connection()
    conn.execute("INSERT INTO config_changes (config_key, old_value, new_value, source) VALUES ('SYMBOL_SETTINGS', ?, ?, 'dashboard')", (str(old), str(table)))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "symbol": sym, "cleared": old})


@app.route("/api/symbols")
def api_symbols():
    """Current trading symbols + enabled state (from config, hot-reloaded)."""
    import scanner
    watchlist = list(dict.fromkeys(
        [s.upper().strip() for s in (config.WATCHLIST or []) + list(getattr(config, "USER_WATCHLIST_OVERRIDE", []) or [])]
        if (config.WATCHLIST or []) or getattr(config, "USER_WATCHLIST_OVERRIDE", [])
        else []
    ))
    disabled = set(getattr(config, "SYMBOLS_DISABLED", []) or [])
    max_active = int(getattr(config, "MAX_ACTIVE_SYMBOLS", 12) or 12)
    enabled_count = sum(1 for s in watchlist if s not in disabled)
    return jsonify({
        "symbols": [{"symbol": s, "enabled": s not in disabled} for s in watchlist],
        "disabled": sorted(disabled),
        "enabled_count": enabled_count,
        "max_active": max_active,
    })


@app.route("/api/symbols/toggle", methods=["POST"])
def api_symbols_toggle():
    """Enable/disable a symbol for trading (persisted to config.py)."""
    data = request.json or {}
    sym = (data.get("symbol") or "").upper().strip()
    if not sym:
        return jsonify({"success": False, "error": "no symbol"}), 400
    enabled = bool(data.get("enabled"))
    disabled = list(getattr(config, "SYMBOLS_DISABLED", []) or [])
    changed = False
    if enabled and sym in disabled:
        # Enforce MAX_ACTIVE_SYMBOLS on re-enable
        watchlist = [s.upper().strip() for s in (config.WATCHLIST or [])]
        max_active = int(getattr(config, "MAX_ACTIVE_SYMBOLS", 12) or 12)
        enabled_count = sum(1 for s in watchlist if s not in disabled)
        if enabled_count >= max_active:
            return jsonify({"success": False, "error": f"Max active symbols reached ({enabled_count}/{max_active}). Disable one first or raise MAX_ACTIVE_SYMBOLS in settings."}), 400
        disabled.remove(sym)
        changed = True
    elif not enabled and sym not in disabled:
        disabled.append(sym)
        changed = True
    if changed:
        old, new = config_loader.set_value("SYMBOLS_DISABLED", disabled)
        conn = get_connection()
        conn.execute("""
            INSERT INTO config_changes (config_key, old_value, new_value, source)
            VALUES ('SYMBOLS_DISABLED', ?, ?, 'dashboard')
        """, (str(old), str(new)))
        conn.commit()
        conn.close()
    return jsonify({"success": True, "symbol": sym, "enabled": enabled})


@app.route("/api/symbols/add", methods=["POST"])
def api_symbols_add():
    """Add a symbol to the trading watchlist (persisted to config.py)."""
    data = request.json or {}
    syms = data.get("symbols") or [data.get("symbol")]
    syms = [s.upper().strip() for s in syms if s and s.strip()]
    if not syms:
        return jsonify({"success": False, "error": "no symbol"}), 400
    wl = list(config.WATCHLIST or [])
    added = [s for s in syms if s not in wl]
    if added:
        # Enforce MAX_ACTIVE_SYMBOLS on add (added symbols start enabled)
        max_active = int(getattr(config, "MAX_ACTIVE_SYMBOLS", 12) or 12)
        disabled = set(getattr(config, "SYMBOLS_DISABLED", []) or [])
        enabled_count = sum(1 for s in wl if s not in disabled)
        if enabled_count + len(added) > max_active:
            return jsonify({"success": False, "error": f"Max active symbols reached ({enabled_count}/{max_active}). Disable one first or raise MAX_ACTIVE_SYMBOLS in settings."}), 400
        old, new = config_loader.set_value("WATCHLIST", wl + added)
        conn = get_connection()
        conn.execute("""
            INSERT INTO config_changes (config_key, old_value, new_value, source)
            VALUES ('WATCHLIST', ?, ?, 'dashboard')
        """, (str(old), str(new)))
        conn.commit()
        conn.close()
    return jsonify({"success": True, "added": added, "watchlist": wl + added})


@app.route("/api/symbols/remove", methods=["POST"])
def api_symbols_remove():
    """Remove a symbol from the trading watchlist (persisted to config.py).
    Also clears its disabled flag so a re-add starts enabled."""
    data = request.json or {}
    sym = (data.get("symbol") or "").upper().strip()
    if not sym:
        return jsonify({"success": False, "error": "no symbol"}), 400
    wl = list(config.WATCHLIST or [])
    if sym in wl:
        wl.remove(sym)
        old, new = config_loader.set_value("WATCHLIST", wl)
        conn = get_connection()
        conn.execute("""
            INSERT INTO config_changes (config_key, old_value, new_value, source)
            VALUES ('WATCHLIST', ?, ?, 'dashboard')
        """, (str(old), str(new)))
        conn.commit()
        conn.close()
    disabled = list(getattr(config, "SYMBOLS_DISABLED", []) or [])
    if sym in disabled:
        disabled.remove(sym)
        config_loader.set_value("SYMBOLS_DISABLED", disabled)
    return jsonify({"success": True, "removed": sym, "watchlist": wl})


@app.route("/auth")
def auth():
    """Show MFA prompt if login is waiting for verification."""
    client = get_client()
    if not client.requires_mfa:
        return redirect(url_for("index"))
    return render_template("auth.html")


@app.route("/api/auth/submit", methods=["POST"])
def api_auth_submit():
    """Submit MFA code from dashboard."""
    data = request.json
    mfa_code = data.get("code", "").strip()
    if not mfa_code:
        return jsonify({"success": False, "error": "no code"}), 400
    client = get_client()
    result = client.login(mfa_code=mfa_code)
    return jsonify(result)





@app.route("/backtest")
def backtest():
    """Backtest results visualization page."""
    return render_template("backtest.html")


@app.route("/api/backtest/runs")
def api_backtest_runs():
    """List all backtest result files."""
    files = sorted(BACKTEST_RESULTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    runs = []
    for f in files:
        try:
            with open(f) as fp:
                d = json_module.load(fp)
            summary = d.get("summary", {})
            label = f"{f.stem} | {summary.get('total_trades', '?')} trades | ${summary.get('total_pnl', 0):.0f}"
            runs.append({"filename": f.name, "label": label})
        except Exception:
            runs.append({"filename": f.name, "label": f.stem})
    return jsonify(runs)


@app.route("/api/backtest/run/<path:filename>")
def api_backtest_run(filename):
    """Load a specific backtest result file."""
    fpath = BACKTEST_RESULTS_DIR / filename
    if not fpath.exists() or not fpath.is_file():
        return jsonify({"error": "not found"}), 404
    try:
        with open(fpath) as f:
            return jsonify(json_module.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@app.route("/review")
def review():
    """Weekly review visualization page."""
    return render_template("review.html")


@app.route("/api/review")
def api_review():
    """Get weekly review data for chart page."""
    try:
        sys.path.insert(0, "/home/ralph/trader-v2/scripts")
        from journal import weekly_review
        return jsonify(weekly_review(days=7))
    except Exception as e:
        return jsonify({"error": str(e), "total_trades": 0}), 500


@app.route("/lifecycle")
def lifecycle_page():
    """Per-symbol trade lifecycle audit page (2026-09-17)."""
    return render_template("lifecycle.html")


@app.route("/api/lifecycle")
def api_lifecycle():
    """Per-symbol trade lifecycle data: bid placement, fills, sell placement,
    first profit / first loss timestamps, peak/trough, exit reason."""
    conn = get_connection()
    cols = ["id", "timestamp_open", "timestamp_close", "symbol", "option_type",
            "option_strike", "option_expiry", "quantity", "entry_price", "exit_price",
            "pnl", "net_pnl", "exit_reason",
            "bid_at_open", "ask_at_open", "bid_place_time", "bid_place_price", "bid_fill_time",
            "sell_place_time", "sell_place_price",
            "time_first_profit", "price_first_profit",
            "time_first_loss", "price_first_loss",
            "peak_price", "peak_time", "trough_price", "trough_time"]
    rows = conn.execute(f"""
        SELECT {', '.join(cols)}
        FROM trades
        ORDER BY timestamp_open DESC LIMIT 500
    """).fetchall()
    conn.close()

    from datetime import datetime as _dt
    def _fmt(v):
        if not v:
            return None
        try:
            d = _dt.fromisoformat(str(v))
            return d.strftime("%H:%M:%S") if d.date() == _dt.now().date() else d.strftime("%m-%d %H:%M:%S")
        except Exception:
            return str(v)

    trades = []
    for r in rows:
        d = dict(zip(cols, r))
        d["timestamp_open_f"] = _fmt(d["timestamp_open"])
        d["timestamp_close_f"] = _fmt(d["timestamp_close"])
        d["bid_place_time_f"] = _fmt(d["bid_place_time"])
        d["bid_fill_time_f"] = _fmt(d["bid_fill_time"])
        d["sell_place_time_f"] = _fmt(d["sell_place_time"])
        d["time_first_profit_f"] = _fmt(d["time_first_profit"])
        d["time_first_loss_f"] = _fmt(d["time_first_loss"])
        d["peak_time_f"] = _fmt(d["peak_time"])
        d["trough_time_f"] = _fmt(d["trough_time"])
        trades.append(d)

    # Group by symbol, newest first within each symbol
    by_symbol = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)

    return jsonify({"symbols": dict(sorted(by_symbol.items())), "total": len(trades)})


if __name__ == "__main__":
    # Login on startup so pending orders, market data, etc. work
    try:
        client = get_client()
        login_result = client.login()
        print(f"Startup login: {login_result}")
    except Exception as e:
        print(f"Startup login failed (non-fatal): {e}")
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False)
