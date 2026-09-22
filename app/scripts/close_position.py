"""Manually close the MU position with a paper sell at current bid."""
import sys
import os
sys.path.insert(0, '/home/ralph/trader-v2')
sys.path.insert(0, '/home/ralph/trader-v2/scripts')

# Force paper mode
os.environ['TRADER_CONFIG_FILE'] = 'config.py'

import importlib.util
spec = importlib.util.spec_from_file_location('cfg', '/home/ralph/trader-v2/config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)

from database import get_connection
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")

# Find the MU position
conn = get_connection()
pos = conn.execute("SELECT * FROM positions WHERE symbol='MU' AND source='system_paper'").fetchone()
conn.close()

if not pos:
    print("No MU position to close")
    sys.exit(0)

print(f"Found MU position: id={pos['id']}, strike={pos['option_strike']}, entry_price={pos['entry_price']}")

# Get current bid from Robinhood
from robinhood_client import get_client
client = get_client()
client._ensure_logged_in()

# The actual option is MU 935 call expiring 2026-08-14 (according to the DB)
import robin_stocks.robinhood as r
market = client.get_option_market_data(
    'MU', pos['option_expiry'], pos['option_strike'], 'call'
)
if not market or market.get('bid', 0) <= 0:
    # Fallback: use the last_poll_price from DB
    print(f"  No market data, using last_poll_price ${pos['last_poll_price']}")
    exit_price = pos['last_poll_price']
else:
    bid = market.get('bid', 0)
    print(f"  Current bid: ${bid}")
    exit_price = bid

# Compute P&L
entry = pos['entry_price']
qty = pos['quantity']
pnl = (exit_price - entry) * qty * 100
print(f"  Entry: ${entry}, Exit: ${exit_price}, Qty: {qty}, P&L: ${pnl:+,.2f}")

# Close the position
conn = get_connection()
trade_id = pos['trade_id'] if 'trade_id' in pos.keys() else None
# Get the related trade row
trade = conn.execute("SELECT id FROM trades WHERE id=? AND timestamp_close IS NULL", (pos['id'],)).fetchone()
# Actually find the trade by 'positions' table - need to find trade that owns this position
trade = conn.execute("SELECT id FROM trades WHERE symbol='MU' AND timestamp_close IS NULL ORDER BY id DESC LIMIT 1").fetchone()
if trade:
    trade_id = trade['id']
    conn.execute("""
        UPDATE trades
        SET timestamp_close=?, exit_price=?, exit_reason=?, pnl=?
        WHERE id=?
    """, (datetime.now(ET).isoformat(), exit_price, 'manual_close', pnl, trade_id))
    print(f"  Updated trade {trade_id}")

# Delete the position
conn.execute("DELETE FROM positions WHERE id=?", (pos['id'],))
conn.commit()
conn.close()

print(f"\n✓ MU position closed at ${exit_price}, P&L: ${pnl:+,.2f}")
