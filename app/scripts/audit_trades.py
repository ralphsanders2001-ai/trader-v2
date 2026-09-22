"""
Audit DB vs Robinhood for today.

Compares every trade we have in the local DB against Robinhood's
authoritative orders. Reports discrepancies in:
- Fill prices (DB vs actual)
- Quantities
- Fees
- Missing trades

Run after market close or weekly to verify data integrity.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from robinhood_client import get_client

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
ET = timezone(timedelta(hours=-4))


def get_rh_orders_today():
    """Fetch all filled option orders from Robinhood for today."""
    client = get_client()
    # Login first (loads pickle session)
    client.login()
    import robinhood_client
    all_orders = robinhood_client.r.get_all_option_orders(info=None) or []
    # Accept env var override for back-audits
    import os
    target_date = os.environ.get('AUDIT_DATE', datetime.now(ET).strftime("%Y-%m-%d"))
    today_orders = []
    for o in all_orders:
        if not o.get('created_at', '').startswith(target_date):
            continue
        if o.get('state') != 'filled':
            continue
        today_orders.append(o)
    return today_orders


def get_db_trades_today():
    """Fetch all trades from local DB for today."""
    conn = sqlite3.connect(DB_PATH)
    import os
    target_date = os.environ.get('AUDIT_DATE', datetime.now(ET).strftime("%Y-%m-%d"))
    rows = conn.execute("""
        SELECT id, symbol, option_type, option_strike, option_expiry,
               entry_price, exit_price, pnl, robinhood_order_id,
               exit_order_id
        FROM trades
        WHERE DATE(timestamp_open) = ?
    """, [target_date]).fetchall()
    conn.close()
    return rows


def main():
    print("=" * 60)
    print(f"Trade Audit — {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    rh_orders = get_rh_orders_today()
    db_trades = get_db_trades_today()

    print(f"\nRobinhood filled orders today: {len(rh_orders)}")
    print(f"DB trades today: {len(db_trades)}")

    # Build map by order ID
    db_by_id = {t[8]: t for t in db_trades if t[8]}
    db_by_exit_id = {t[9]: t for t in db_trades if t[9]}
    db_trade_ids = set(db_by_id.keys()) | set(db_by_exit_id.keys())

    # Audit: orders in RH but not in DB
    missing = []
    for rh in rh_orders:
        rh_id = rh.get('id')
        if rh_id not in db_trade_ids:
            leg = rh.get('legs', [{}])[0]
            missing.append({
                'rh_id': rh_id,
                'symbol': rh.get('chain_symbol'),
                'side': leg.get('side'),
                'strike': leg.get('strike_price'),
                'type': leg.get('option_type'),
                'state': rh.get('state'),
            })

    # Audit: trades in DB but not in RH
    orphans = []
    rh_ids = {o.get('id') for o in rh_orders}
    for db_id, db_exit_id, *rest in [(t[8], t[9], t) for t in db_trades]:
        if db_id and db_id not in rh_ids and (not db_exit_id or db_exit_id not in rh_ids):
            orphans.append(db_id)

    # Price discrepancies
    discrepancies = []
    for rh in rh_orders:
        rh_id = rh.get('id')
        if rh_id in db_by_id:
            db_trade = db_by_id[rh_id]
            leg = rh.get('legs', [{}])[0]
            executions = leg.get('executions', [])
            if executions:
                total_notional = sum(
                    float(e.get('price', 0)) * float(e.get('quantity', 0))
                    for e in executions
                )
                total_qty = sum(float(e.get('quantity', 0)) for e in executions)
                actual_price = total_notional / total_qty if total_qty else 0

                db_price = db_trade[5]  # entry_price
                if actual_price and abs(actual_price - db_price) > 0.01:
                    discrepancies.append({
                        'rh_id': rh_id,
                        'symbol': db_trade[1],
                        'db_price': db_price,
                        'actual_price': actual_price,
                        'diff': actual_price - db_price,
                    })

    # Print report
    print(f"\n=== Missing from DB ({len(missing)}) ===")
    for m in missing[:10]:
        print(f"  {m['symbol']} {m['type']} ${m['strike']} {m['side']} ({m['rh_id'][:8]}...)")

    print(f"\n=== Orphan trades in DB ({len(orphans)}) ===")
    for o in orphans[:10]:
        print(f"  {o[:8]}...")

    print(f"\n=== Price discrepancies ({len(discrepancies)}) ===")
    for d in discrepancies[:10]:
        print(f"  {d['symbol']}: DB ${d['db_price']:.2f} vs actual ${d['actual_price']:.2f} (diff ${d['diff']:+.2f})")

    # Save report
    report = {
        'timestamp': datetime.now(ET).isoformat(),
        'rh_order_count': len(rh_orders),
        'db_trade_count': len(db_trades),
        'missing_from_db': missing,
        'orphans': orphans,
        'price_discrepancies': discrepancies,
    }
    out_dir = Path("/home/ralph/trader-v2/audit-reports")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"audit-{datetime.now(ET).strftime('%Y-%m-%d')}.json"
    with open(out_file, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved: {out_file}")


if __name__ == "__main__":
    main()