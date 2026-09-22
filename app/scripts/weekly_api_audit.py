"""
Weekly Robinhood API Field Audit

Verifies our trader code uses all available Robinhood API fields
correctly. Reports any new fields or changes in field values.

Run weekly to catch Robinhood API drift.

Output:
- /home/ralph/trader-v2/api-audit-reports/api-audit-YYYY-MM-DD.json
- /home/ralph/trader-v2/api-audit-reports/api-audit-YYYY-MM-DD.txt (summary)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/home/ralph/trader-v2/scripts")

from robinhood_client import get_client

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
ET = timezone(timedelta(hours=-4))

# Known fields at time of audit (update when Robinhood adds new fields)
KNOWN_ORDER_FIELDS = {
    'account_number', 'account_number_rhs', 'cancel_url', 'canceled_quantity',
    'created_at', 'derived_state', 'direction', 'estimated_total_net_amount',
    'estimated_total_net_amount_direction', 'estimated_total_net_amount_direction_v2',
    'estimated_total_net_amount_v2', 'form_source', 'gold_savings', 'id',
    'is_replaceable', 'legs', 'market_hours', 'net_amount', 'net_amount_direction',
    'opening_strategy', 'pending_quantity', 'placed_agent', 'premium', 'price',
    'processed_premium', 'processed_premium_direction', 'processed_quantity',
    'quantity', 'ref_id', 'regulatory_fees', 'contract_fees', 'sales_taxes',
    'state', 'stop_price', 'strategy', 'time_in_force', 'trade_value_multiplier',
    'trigger', 'type', 'updated_at', 'agent_display_name', 'agent_id',
    'canceled_agent_id', 'canceled_agent_name', 'chain_id', 'chain_symbol',
    'closing_strategy', 'client_ask_at_submission', 'client_bid_at_submission',
    'client_time_at_submission', 'response_category', 'average_net_premium_paid',
}


def fetch_sample_orders():
    """Fetch filled orders from the last 7 days for field discovery."""
    client = get_client()
    client.login()
    import robinhood_client
    all_orders = robinhood_client.r.get_all_option_orders(info=None) or []

    cutoff = (datetime.now(ET) - timedelta(days=7)).strftime("%Y-%m-%d")
    sample = [o for o in all_orders
              if o.get('created_at', '').startswith(cutoff[:7])]  # last month
    sample = [o for o in sample if o.get('state') == 'filled'][:5]
    return sample


def discover_fields(orders):
    """Discover all fields present in real orders."""
    order_fields = set()
    leg_fields = set()
    exec_fields = set()
    states = set()

    for o in orders:
        order_fields.update(o.keys())
        states.add(o.get('state'))
        for leg in o.get('legs', []):
            leg_fields.update(leg.keys())
            for exec_data in leg.get('executions', []):
                exec_fields.update(exec_data.keys())

    return order_fields, leg_fields, exec_fields, states


def check_trader_usage():
    """Check which fields our trader code actually uses."""
    script_dir = Path("/home/ralph/trader-v2/scripts")
    code = ""
    for py_file in script_dir.glob("*.py"):
        code += py_file.read_text()

    # Find .get('field_name') and .get("field_name") patterns
    used = set()
    for match in re.finditer(r'\.get\([\'"]([\w_]+)[\'"]', code):
        used.add(match.group(1))

    return used


def main():
    print("=" * 60)
    print(f"Weekly API Audit — {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    orders = fetch_sample_orders()
    if not orders:
        print("No recent orders found — cannot audit")
        return

    order_fields, leg_fields, exec_fields, states = discover_fields(orders)
    used_in_code = check_trader_usage()

    # Find fields we should use but don't
    unused = (order_fields | leg_fields | exec_fields) - used_in_code
    missing_in_kb = order_fields - KNOWN_ORDER_FIELDS

    print(f"\nOrders sampled: {len(orders)}")
    print(f"Order-level fields seen: {len(order_fields)}")
    print(f"Leg-level fields seen: {len(leg_fields)}")
    print(f"Execution-level fields seen: {len(exec_fields)}")
    print(f"States seen: {states}")
    print(f"\nFields in trader code: {len(used_in_code)}")
    print(f"Fields available but unused: {len(unused)}")
    print(f"NEW fields not in our knowledge base: {len(missing_in_kb)}")

    if missing_in_kb:
        print("\n=== NEW FIELDS DETECTED ===")
        for f in sorted(missing_in_kb):
            print(f"  {f}")

    if unused and len(unused) < 20:
        print("\n=== AVAILABLE FIELDS NOT USED ===")
        for f in sorted(unused):
            print(f"  {f}")

    # Save report
    out_dir = Path("/home/ralph/trader-v2/api-audit-reports")
    out_dir.mkdir(exist_ok=True)
    today = datetime.now(ET).strftime("%Y-%m-%d")

    report = {
        'timestamp': datetime.now(ET).isoformat(),
        'orders_sampled': len(orders),
        'order_fields_count': len(order_fields),
        'leg_fields_count': len(leg_fields),
        'exec_fields_count': len(exec_fields),
        'states_seen': list(states),
        'fields_in_trader_code': len(used_in_code),
        'available_fields_unused': sorted(unused),
        'new_fields_detected': sorted(missing_in_kb),
    }

    json_file = out_dir / f"api-audit-{today}.json"
    with open(json_file, 'w') as f:
        json.dump(report, f, indent=2)

    # Also save to NAS
    nas_dir = Path("/mnt/file-cabinet/trader-v2/API/weekly-audits")
    nas_dir.mkdir(parents=True, exist_ok=True)
    nas_file = nas_dir / f"api-audit-{today}.json"
    with open(nas_file, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nReport saved:")
    print(f"  Local: {json_file}")
    print(f"  NAS: {nas_file}")


if __name__ == "__main__":
    main()