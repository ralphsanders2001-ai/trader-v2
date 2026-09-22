"""
Continuous Robinhood Sync Service

Periodically pulls option order history from Robinhood and rebuilds the
local DB to match exactly. This is the source of truth — Robinhood says
what happened, the local DB just records it.

Runs as a separate background process so it doesn't interfere with the
main trader daemon.

Sync strategy:
1. Fetch all filled option orders for today from Robinhood
2. Match buys with sells using FIFO (preserves round-trip economics)
3. Wipe today's local trade rows and positions
4. Insert authoritative rows with correct P&L
5. Refresh dashboard data

Schedule: every 60 seconds during market hours.
"""
import os
import sys
import time
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

# Add scripts dir to path
sys.path.insert(0, "/home/ralph/trader-v2/scripts")

from robinhood_client import get_client
import robin_stocks.robinhood as r

# 2026-08-10: sync writes to live DB only (paper has its own DB)
import config_live as config
DB_PATH = config.DB_PATH
ET = timezone(timedelta(hours=-4))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [sync] %(levelname)s: %(message)s',
)
log = logging.getLogger("sync")


def to_et(utc_str):
    dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
    return dt.astimezone(ET).isoformat()


def fetch_orders_today(client):
    """Fetch all filled option orders from Robinhood for today."""
    # The local wrapper doesn't expose get_all_option_orders, so we use the
    # underlying robin_stocks library directly.
    try:
        import robin_stocks.robinhood as r
        # Force login first
        client._ensure_logged_in()
        orders = r.get_all_option_orders(info=None) or []
    except Exception as e:
        log.error(f"Could not fetch orders: {e}")
        return []

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    today = []
    for o in orders:
        created = o.get('created_at', '')
        state = o.get('state', '')
        if not created.startswith(today_str):
            continue
        if state != 'filled':
            continue
        today.append(o)
    # Sort chronologically
    today.sort(key=lambda x: x['created_at'])
    return today


def fifo_match(orders):
    """Match buys with sells using FIFO and return completed round-trips."""
    lots = []
    completed = []

    for o in orders:
        leg = o['legs'][0]
        side = leg['side']
        # Use processed_quantity (filled amount) not quantity (limit)
        qty = int(float(o.get('processed_quantity', 0) or 0))
        if qty == 0:
            # Fallback to quantity if processed is missing
            qty = int(float(o.get('quantity', 0)))

        # Compute actual fill price from executions (not the limit price)
        total_notional = 0.0
        total_filled = 0.0
        for exec_data in leg.get('executions', []):
            eqty = float(exec_data.get('quantity', 0) or 0)
            eprice = float(exec_data.get('price', 0) or 0)
            total_notional += eqty * eprice
            total_filled += eqty
        price = (total_notional / total_filled) if total_filled > 0 else float(o.get('price', 0))

        net = float(o.get('net_amount', 0))
        fees = float(o.get('regulatory_fees', 0) or 0)
        fees += float(o.get('contract_fees', 0) or 0)  # Add contract fees
        strike = float(leg['strike_price'])
        opt_type = leg['option_type']
        # FIX 2026-08-10: Robinhood's placed_agent='user' for ALL our API orders
        # (same auth path). Use agent_display_name as the real signal:
        #   - null/empty → API order → 'bot'
        #   - real name  → manual app order → 'user'
        rh_agent = o.get('placed_agent', 'unknown')
        agent_display = o.get('agent_display_name') or o.get('canceled_agent_name') or ''
        if agent_display:
            placed_by = 'user'
        else:
            # No human name → API/bot order
            placed_by = 'bot'

        # Get chain_symbol from instrument (legs don't carry it)
        chain_symbol = ''
        opt_url = leg.get('option', '')
        instrument_id = ''
        if '/instruments/' in opt_url:
            instrument_id = opt_url.split('/instruments/')[1].rstrip('/')
            try:
                instr = r.get_option_instrument_data_by_id(instrument_id)
                if instr:
                    chain_symbol = instr.get('chain_symbol', '')
                else:
                    log.warning(f"No instrument data for {instrument_id}")
            except Exception as e:
                log.warning(f"Could not fetch instrument {instrument_id}: {e}")

        if side == 'buy':
            lots.append({
                'buy_id': o['id'],
                'buy_price': price,
                'buy_net': net,
                'qty': qty,
                'remaining': qty,
                'strike': strike,
                'opt_type': opt_type,
                'expiry': leg.get('expiration_date', ''),
                'symbol': chain_symbol,
                'instrument_id': instrument_id,
                'fees': fees,
                'buy_time': o['created_at'],
                'placed_by': placed_by,
            })
        else:  # sell
            qty_to_close = qty
            sell_net = net
            sell_price = price

            # Find the first matching lot, search through all open lots
            while qty_to_close > 0:
                # Find the next matching lot
                match_idx = None
                for idx, lot in enumerate(lots):
                    if (lot['strike'] == strike and \
                            lot['opt_type'] == opt_type and \
                            lot.get('symbol') == chain_symbol):
                        match_idx = idx
                        break
                if match_idx is None:
                    break  # No matching lot exists

                lot = lots[match_idx]
                if lot['remaining'] <= qty_to_close:
                    closed_qty = lot['remaining']
                    sell_credit = sell_net * (closed_qty / qty)
                    pnl = sell_credit - lot['buy_net'] * (closed_qty / lot['qty'])
                    completed.append({
                        'buy_id': lot['buy_id'],
                        'sell_id': o['id'],
                        'buy_time': lot['buy_time'],
                        'sell_time': o['created_at'],
                        'strike': strike,
                        'opt_type': opt_type,
                        'symbol': chain_symbol,
                        'expiry': lot.get('expiry', ''),
                        'qty': closed_qty,
                        'buy_price': lot['buy_price'],
                        'sell_price': sell_price,
                        'pnl': pnl,
                        'placed_by': placed_by,
                    })
                    qty_to_close -= closed_qty
                    lots.pop(match_idx)
                else:
                    closed_qty = qty_to_close
                    pnl = sell_net - lot['buy_net'] * (closed_qty / lot['qty'])
                    completed.append({
                        'buy_id': lot['buy_id'],
                        'sell_id': o['id'],
                        'buy_time': lot['buy_time'],
                        'sell_time': o['created_at'],
                        'strike': strike,
                        'opt_type': opt_type,
                        'symbol': chain_symbol,
                        'expiry': lot.get('expiry', ''),
                        'qty': closed_qty,
                        'buy_price': lot['buy_price'],
                        'sell_price': sell_price,
                        'pnl': pnl,
                        'placed_by': placed_by,
                    })
                    lot['remaining'] -= closed_qty
                    lot['buy_net'] *= (lot['remaining'] / lot['qty'])
                    qty_to_close = 0

    return completed, lots


def classify_exit(pnl_pct, sell_to_buy_seconds):
    """Classify why a trade closed based on characteristics."""
    if pnl_pct > 0.15:
        return 'profit_target'
    elif pnl_pct < -0.10:
        return 'loss_cap'
    elif sell_to_buy_seconds < 30:
        return 'orphan_sync'  # Closed too fast for human/strategy
    else:
        return 'manual'  # Long hold suggests manual close


def sync_db():
    """Reconcile local DB with Robinhood."""
    try:
        client = get_client()
    except Exception as e:
        log.error(f"Could not connect to Robinhood: {e}")
        return False

    orders = fetch_orders_today(client)
    completed, open_lots = fifo_match(orders)

    from database import get_connection
    # 2026-08-10: don't use database.get_connection() — it uses module-level
    # config which may be paper. Sync must always hit the LIVE DB.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # Wipe today's data — delete signals first due to FK on trade_id
    # 2026-08-10: only wipe LIVE mode rows; paper DB is untouched by sync
    conn.execute("DELETE FROM signals WHERE mode = 'live' AND DATE(timestamp) = ?", (today,))
    conn.execute("DELETE FROM trades WHERE mode = 'live' AND DATE(timestamp_open) = ?", (today,))
    conn.execute("DELETE FROM positions WHERE source = 'rh_sync' AND DATE(entry_time) LIKE ? || '%'", (today,))

    # Insert authoritative trades
    for t in completed:
        buy_et = to_et(t['buy_time'])
        sell_et = to_et(t['sell_time'])
        pnl_rounded = round(t['pnl'], 2)

        buy_dt = datetime.fromisoformat(t['buy_time'].replace('Z', '+00:00'))
        sell_dt = datetime.fromisoformat(t['sell_time'].replace('Z', '+00:00'))
        sell_to_buy_seconds = (sell_dt - buy_dt).total_seconds()
        pnl_pct = (t['sell_price'] - t['buy_price']) / t['buy_price'] if t['buy_price'] else 0
        reason = classify_exit(pnl_pct, sell_to_buy_seconds)

        # Use symbol/expiry captured by FIFO match directly
        sym = t.get('symbol', '')
        expiry = t.get('expiry', '')

        conn.execute("""
            INSERT INTO trades (
                timestamp_open, timestamp_close, mode, symbol, option_type,
                option_strike, option_expiry, quantity, entry_price, exit_price,
                pnl, exit_reason, robinhood_order_id, exit_order_id, placed_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            buy_et, sell_et, 'live', sym, t['opt_type'],
            t['strike'], expiry, t['qty'], t['buy_price'], t['sell_price'],
            pnl_rounded, reason, t['buy_id'], t['sell_id'], t.get('placed_by', 'unknown')
        ))

    # Insert open positions (Robinhood has them open, local DB should too)
    # Only mark as on_hold=1 if the bot doesn't already have a matching position
    # (a system-source position indicates the bot placed it; don't override that
    # with the manual-user default).
    for lot in open_lots:
        # Check if a system-source position exists for this (symbol, strike, expiry)
        existing = conn.execute("""
            SELECT id FROM positions
            WHERE symbol = ? AND option_strike = ? AND option_expiry = ?
            AND source = 'system'
            LIMIT 1
        """, (lot.get('symbol', ''), lot['strike'], lot.get('expiry', ''))).fetchone()

        if existing:
            # Bot owns this position; sync already inserted a system row, skip
            log.debug(f"[sync] Skipping rh_sync insert for {lot.get('symbol')} ${lot['strike']} - already managed by bot")
            continue

        conn.execute("""
            INSERT INTO positions (
                symbol, option_type, option_strike, option_expiry, quantity,
                entry_price, entry_time, robinhood_instrument_id,
                tier, source, on_hold
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            lot.get('symbol', ''), lot['opt_type'], lot['strike'], lot.get('expiry', ''),
            lot['remaining'], lot['buy_price'], to_et(lot['buy_time']),
            lot.get('instrument_id', ''), 'standard', 'rh_sync', 1
        ))

    # Update circuit_breaker.realized_pnl so the main dashboard shows the
    # correct daily P&L. Without this the dashboard reads from a stale field
    # that gets reset by the daemon at midnight.
    total_pnl = sum(t['pnl'] for t in completed)
    conn.execute("""
        UPDATE circuit_breaker
        SET realized_pnl = ?, trading_day = ?, last_updated = ?
        WHERE id = 1
    """, (round(total_pnl, 2), today,
          datetime.now(ET).isoformat()))

    conn.commit()
    conn.close()

    log.info(f"Synced {len(completed)} trades, {len(open_lots)} open, P&L ${total_pnl:+.2f}")
    return True


def main():
    log.info("Robinhood sync service starting")
    while True:
        try:
            sync_db()
        except Exception as e:
            log.error(f"Sync failed: {e}", exc_info=True)
        time.sleep(60)


if __name__ == "__main__":
    main()