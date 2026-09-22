"""
Nightly reset: mirror live account state to paper so both start the next day identical.
Pulls live cash/equity/buying_power and open positions from the database (synced by Agent cron),
then resets paper account to match.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import init_db, get_db
from datetime import datetime, timezone

def sync_live_to_paper():
    init_db()
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        # Pull live account state
        live = conn.execute(
            "SELECT * FROM account_state WHERE mode='live'"
        ).fetchone()
        if not live:
            print("ERROR: No live account state found in DB")
            return

        live = dict(live)

        # Pull open live positions
        live_positions = [
            dict(r) for r in conn.execute(
                "SELECT * FROM trades WHERE mode='live' AND status='open'"
            ).fetchall()
        ]

        # Update paper cash/equity/buying_power to match live
        conn.execute("""
            UPDATE account_state
            SET cash = ?, equity = ?, buying_power = ?,
                buy_count = 0, sell_count = 0,
                count_reset_date = ?, updated_at = ?
            WHERE mode = 'paper'
        """, (live["cash"], live["equity"], live["buying_power"], today, now))

        # Delete existing paper open positions
        conn.execute("DELETE FROM trades WHERE mode='paper' AND status='open'")

        # Copy live open positions to paper
        for pos in live_positions:
            conn.execute("""
                INSERT INTO trades (
                    mode, trade_date, symbol, option_expiry, option_strike,
                    option_type, direction, quantity, entry_price, entry_time,
                    status, fees, notes, created_at
                ) VALUES (
                    'paper', ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, 'open', ?, NULL, ?
                )
            """, (
                today,
                pos["symbol"], pos.get("option_expiry"), pos.get("option_strike"),
                pos.get("option_type"), pos.get("direction", "long_call"),
                pos["quantity"], pos["entry_price"],
                pos.get("entry_time", now),
                pos.get("fees", 0.0),
                now
            ))

        conn.commit()

        paper = conn.execute("SELECT * FROM account_state WHERE mode='paper'").fetchone()

    print(f"Nightly sync complete — {today}")
    print(f"Paper now: cash={live['cash']:.2f} | equity={live['equity']:.2f} | BP={live['buying_power']:.2f}")
    if live_positions:
        for p in live_positions:
            print(f"  Copied: {p['symbol']} {p.get('option_type')} {p.get('option_strike')} x{p['quantity']} @ {p['entry_price']}")
    else:
        print("  No open live positions to copy")

if __name__ == "__main__":
    sync_live_to_paper()
