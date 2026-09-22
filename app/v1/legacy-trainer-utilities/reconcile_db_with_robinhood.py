"""
Reconcile the local trades DB against Robinhood's actual order history.

For each DB row, find the matching Robinhood contract orders and determine
the correct state:
  - DB row matches a filled BUY + filled SELL in Robinhood -> update with
    actual exit_price, exit_time, pnl
  - DB row matches only a CANCELLED BUY in Robinhood -> mark cancelled
    (no money moved, pnl=0)
  - DB row matches a filled BUY but no SELL -> mark open
  - DB row has no matching Robinhood order at all -> flag for review

Usage:
    python3 reconcile_db_with_robinhood.py                    # today
    python3 reconcile_db_with_robinhood.py --date 2026-07-27  # specific date
    python3 reconcile_db_with_robinhood.py --dry-run          # show what would change

Exit code: 0 if everything was clean, 1 if any DB rows were updated, 2 on auth error.
"""
import argparse
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, "/home/ralph/robinhood-trainer")

import robin_stocks.robinhood as r
from rh_credentials import ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD

DB_PATH = "/home/ralph/robinhood-trainer/data/trainer.db"


def login():
    try:
        r.login(username=ROBINHOOD_USERNAME, password=ROBINHOOD_PASSWORD, pickle_name="")
        return True
    except Exception as e:
        print(f"Robinhood login failed: {e}", file=sys.stderr)
        return False


def fetch_today_orders(target_date: str):
    """Fetch all option orders for target_date from Robinhood."""
    orders = r.get_all_option_orders() or []
    results = []
    for o in orders:
        ca = o.get("created_at", "")
        if not ca.startswith(target_date):
            continue
        legs = o.get("legs", [])
        if not legs:
            continue
        leg = legs[0]
        instr_url = leg.get("option")
        symbol = strike = opt_type = expiration = None
        if instr_url:
            try:
                instr_data = r.request_get(instr_url)
                if instr_data:
                    symbol = instr_data.get("chain_symbol")
                    strike = instr_data.get("strike_price")
                    opt_type = instr_data.get("type")
                    expiration = instr_data.get("expiration_date")
            except Exception:
                pass
        try:
            results.append({
                "id": o.get("id", "")[:8],
                "created_at": ca,
                "state": o.get("state"),
                "side": leg.get("side"),
                "position_effect": leg.get("position_effect"),
                "symbol": symbol or "?",
                "strike": float(strike) if strike else 0.0,
                "opt_type": opt_type or "call",
                "expiration": expiration or "?",
                "price": float(o.get("price", 0)),
                "premium": float(o.get("processed_premium", 0)),
                "quantity": int(float(o.get("quantity", 1))) if o.get("quantity") else 1,
            })
        except (ValueError, TypeError):
            continue
    return results


def load_db_rows(target_date: str):
    """Load DB rows for the given date."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT id, symbol, option_strike, option_type, option_expiry, direction,
               quantity, entry_price, entry_time, exit_price, exit_time,
               status, pnl, fees, notes
        FROM trades
        WHERE trade_date = ?
        ORDER BY id
    """, (target_date,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def key(symbol, strike, opt_type, expiration):
    return (symbol, float(strike) if strike else 0.0, opt_type or "call", expiration or "?")


def _parse_iso(s):
    """Parse ISO datetime string, return None on failure."""
    if not s:
        return None
    try:
        if isinstance(s, str):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except (ValueError, TypeError):
        return None


def reconcile(target_date: str, dry_run: bool = False):
    print(f"=== Reconciling {target_date} (dry_run={dry_run}) ===\n")

    db_rows = load_db_rows(target_date)
    rh_orders = fetch_today_orders(target_date)

    # Build a dict of {key: [filled_orders, cancelled_orders]}
    rh_fills = {}    # key -> list of filled orders
    rh_cancels = {}  # key -> list of cancelled BUY orders
    for o in rh_orders:
        k = key(o["symbol"], o["strike"], o["opt_type"], o["expiration"])
        if o["state"] == "filled":
            rh_fills.setdefault(k, []).append(o)
        elif o["state"] == "cancelled" and o["position_effect"] == "open":
            rh_cancels.setdefault(k, []).append(o)

    updates = []  # (row_id, new_values_dict, reason_text)

    for row in db_rows:
        k = key(row["symbol"], row["option_strike"], row["option_type"], row["option_expiry"])
        fills = rh_fills.get(k, [])
        cancels = rh_cancels.get(k, [])

        new_values = {}
        reason = ""

        if not fills and not cancels:
            # No matching Robinhood order at all
            new_values["notes"] = "WARNING: no matching order in Robinhood — manual review needed"
            reason = "no Robinhood order"

        elif not fills and cancels:
            # Only cancelled orders — DB row should be marked as cancelled
            new_values["status"] = "cancelled"
            new_values["exit_price"] = None
            new_values["exit_time"] = None
            new_values["pnl"] = 0.0
            new_values["notes"] = f"reconciled: order was cancelled in Robinhood (never filled, no money moved)"
            reason = f"Robinhood order was cancelled (no fill)"

        else:
            # We have fills (and possibly some cancels).
            # Match by entry_time proximity: pick the BUY that matches the DB row's entry_time.
            buys = sorted([o for o in fills if o["position_effect"] == "open"],
                          key=lambda x: x["created_at"])
            sells = sorted([o for o in fills if o["position_effect"] == "close"],
                           key=lambda x: x["created_at"])

            if not buys:
                new_values["notes"] = "WARNING: filled orders exist but no open position — review"
                reason = "no buy in fills"
            else:
                # Pick the BUY whose created_at is closest to the DB entry_time.
                # If multiple DB rows match the same contract, this picks the right one.
                db_entry_iso = row["entry_time"]
                buy = min(buys,
                          key=lambda x: abs(
                              _parse_iso(x["created_at"]) - _parse_iso(db_entry_iso)
                          ) if _parse_iso(x["created_at"]) and _parse_iso(db_entry_iso)
                          else 10**9)

                # If this DB row's entry_time matches a CANCELLED order more closely
                # than any filled BUY, treat it as cancelled (phantom duplicate).
                if cancels:
                    cancelled_buy = min(cancels,
                                        key=lambda x: abs(
                                            _parse_iso(x["created_at"]) - _parse_iso(db_entry_iso)
                                        ) if _parse_iso(x["created_at"]) and _parse_iso(db_entry_iso)
                                        else 10**9)
                    buy_delta = abs(_parse_iso(buy["created_at"]) - _parse_iso(db_entry_iso)) \
                        if _parse_iso(buy["created_at"]) and _parse_iso(db_entry_iso) else 10**9
                    cancel_delta = abs(_parse_iso(cancelled_buy["created_at"]) - _parse_iso(db_entry_iso)) \
                        if _parse_iso(cancelled_buy["created_at"]) and _parse_iso(db_entry_iso) else 10**9
                    if cancel_delta < buy_delta:
                        # This DB row matches a cancelled order, not the filled one
                        new_values["status"] = "cancelled"
                        new_values["exit_price"] = None
                        new_values["exit_time"] = None
                        new_values["pnl"] = 0.0
                        new_values["notes"] = "reconciled: matches cancelled Robinhood order (phantom duplicate)"
                        reason = "matches cancelled order (phantom duplicate)"
                        if new_values:
                            updates.append((row["id"], new_values, reason))
                        continue

                # Update entry fields if DB was wrong.
                # IMPORTANT: entry_price is per-share of underlying. Convert
                # from total premium (processed_premium / 100) rather than
                # the limit-order price field, which can differ from the fill
                # price when the order filled below the limit.
                buy_fill_per_share = buy["premium"] / 100
                if abs(row["entry_price"] - buy_fill_per_share) > 0.001:
                    new_values["entry_price"] = buy_fill_per_share
                if row["quantity"] != buy["quantity"]:
                    new_values["quantity"] = buy["quantity"]

                if sells:
                    sell = sells[0]
                    sell_fill_per_share = sell["premium"] / 100
                    new_values["exit_price"] = sell_fill_per_share
                    new_values["exit_time"] = sell["created_at"]
                    pnl = round(sell["premium"] - buy["premium"], 2)
                    if row["pnl"] != pnl:
                        new_values["pnl"] = pnl
                    new_values["status"] = "closed"
                    new_values["notes"] = (
                        f"reconciled from Robinhood: buy=${buy['premium']:.2f} "
                        f"sell=${sell['premium']:.2f} pnl=${pnl:+.2f}"
                    )
                    reason = f"closed: ${buy['premium']:.2f} -> ${sell['premium']:.2f} = ${pnl:+.2f}"
                else:
                    new_values["status"] = "open"
                    new_values["notes"] = f"reconciled: open position bought at ${buy['premium']:.2f}"
                    reason = f"open: bought at ${buy['premium']:.2f}"

        if new_values:
            updates.append((row["id"], new_values, reason))

    if not updates:
        print("No updates needed — DB matches Robinhood.\n")
        return 0

    print(f"{len(updates)} update(s) planned:\n")
    for row_id, new_values, reason in updates:
        print(f"  Row {row_id}: {reason}")
        if new_values:
            for col, val in new_values.items():
                print(f"    {col} = {val!r}")
        print()

    if dry_run:
        print("DRY RUN — no changes applied.\n")
    else:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for row_id, new_values, _reason in updates:
            if not new_values:
                continue
            set_clauses = []
            params = []
            for col, val in new_values.items():
                set_clauses.append(f"{col} = ?")
                params.append(val)
            params.append(row_id)
            sql = f"UPDATE trades SET {', '.join(set_clauses)} WHERE id = ?"
            c.execute(sql, params)
        conn.commit()
        conn.close()
        print(f"{len(updates)} update(s) committed to {DB_PATH}\n")

    return 1 if any(u[1] for u in updates) else 0


def main():
    parser = argparse.ArgumentParser(description="Reconcile local DB with Robinhood order history")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="Date to reconcile (YYYY-MM-DD), default today")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying DB")
    args = parser.parse_args()

    if not login():
        sys.exit(2)

    rc = reconcile(args.date, dry_run=args.dry_run)
    sys.exit(rc)


if __name__ == "__main__":
    main()