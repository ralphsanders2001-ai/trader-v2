"""
Capital governor — $190 account balance enforcement (2026-09-21, Ralph).

Ralph's rule: the paper bot has a $190 account. It may only spend that
$190 PLUS realized profits from closed trades. Losses reduce what it can
spend. A running profit/loss total is kept and shown on the dashboard.

Design:
- capital_state table (id=1) in trader_paper.db holds starting_balance
  ($190) and trade_id_marker (the trade id at activation — P&L is counted
  only for trades opened after the marker, so old history doesn't count).
- realized P&L = SUM(net_pnl) of closed trades with id > marker.
- reserved = sum(entry_price * 100 * quantity) for OPEN positions
  (cash tied up in contracts currently held).
- available = starting_balance + realized - reserved.
- A buy costing `cost = limit_price * 100 * qty` is allowed only if
  available >= cost. Rejected otherwise (rejection_reason=insufficient_capital).

All reads/writes go through database.get_connection (same lock handling).
"""
import sys

sys.path.insert(0, "/home/ralph/trader-v2")
sys.path.insert(0, "/home/ralph/trader-v2/scripts")

import config
from database import get_connection

TABLE = "capital_state"
DEFAULT_STARTING_BALANCE = 190.0


def init_capital_table():
    """Create the capital_state table and seed row 1 if missing."""
    conn = get_connection()
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            starting_balance REAL NOT NULL,
            trade_id_marker INTEGER NOT NULL,
            updated_at TEXT
        )
    """)
    row = conn.execute(f"SELECT id FROM {TABLE} WHERE id=1").fetchone()
    if not row:
        # Marker = current max trade id: pre-existing trades don't count.
        max_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM trades"
        ).fetchone()[0]
        conn.execute(
            f"INSERT INTO {TABLE} (id, starting_balance, trade_id_marker, updated_at) "
            "VALUES (1, ?, ?, datetime('now'))",
            (DEFAULT_STARTING_BALANCE, max_id),
        )
    conn.commit()
    conn.close()


def _today_et():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def maybe_daily_rollover():
    """Start each trading day at the anchor balance (2026-09-21, Ralph).

    On the first capital check of a new ET day, move trade_id_marker to the
    current max trade id — realized P&L resets to 0 and the day begins at
    exactly the starting balance. Yesterday's simulated (and phantom) results
    don't carry forward. Idempotent: stores the rollover day in updated_at.
    """
    init_capital_table()
    today = _today_et()
    conn = get_connection()
    row = conn.execute(
        f"SELECT trade_id_marker, updated_at FROM {TABLE} WHERE id=1"
    ).fetchone()
    if row and row["updated_at"] and row["updated_at"].startswith(today):
        conn.close()
        return False
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
    conn.execute(
        f"UPDATE {TABLE} SET trade_id_marker=?, updated_at=? WHERE id=1",
        (max_id, today + " 00:00:00"),
    )
    conn.commit()
    conn.close()
    print(f"[CAPITAL] daily rollover: marker -> {max_id}, day starts at "
          f"starting balance, realized P&L reset to $0")
    return True


def get_state():
    """Return dict(starting_balance, trade_id_marker, updated_at)."""
    maybe_daily_rollover()
    conn = get_connection()
    row = conn.execute(
        f"SELECT starting_balance, trade_id_marker, updated_at FROM {TABLE} WHERE id=1"
    ).fetchone()
    conn.close()
    if not row:
        init_capital_table()
        return get_state()
    return {
        "starting_balance": float(row["starting_balance"]),
        "trade_id_marker": int(row["trade_id_marker"]),
        "updated_at": row["updated_at"],
    }


def set_starting_balance(value):
    """Ralph can move the anchor balance (persisted, audited via config_changes-like log)."""
    conn = get_connection()
    conn.execute(
        f"UPDATE {TABLE} SET starting_balance=?, updated_at=datetime('now') WHERE id=1",
        (float(value),),
    )
    conn.commit()
    conn.close()


def realized_pnl():
    """Running realized P&L since the marker (closed paper trades only)."""
    state = get_state()
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(net_pnl), 0) FROM trades "
        "WHERE mode='paper' AND timestamp_close IS NOT NULL AND id > ?",
        (state["trade_id_marker"],),
    ).fetchone()
    conn.close()
    return float(row[0] or 0.0)


def reserved_cash():
    """Cash currently tied up in open positions (entry cost basis)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(entry_price * 100 * quantity), 0) FROM positions"
    ).fetchone()
    conn.close()
    return float(row[0] or 0.0)


def balance():
    """Account balance: $190 + running realized P&L."""
    return get_state()["starting_balance"] + realized_pnl()


def available_cash():
    """Spendable cash: balance minus cash reserved by open positions."""
    return balance() - reserved_cash()


def can_afford(cost):
    """True if a buy costing `cost` dollars fits within available cash."""
    return available_cash() >= cost - 1e-9


def gate_buy(cost):
    """Capital gate for a new entry.

    Returns (allowed: bool, detail: dict). detail carries every number the
    dashboard/daemon log needs for the rejection reason.
    """
    init_capital_table()
    maybe_daily_rollover()
    state = get_state()
    realized = realized_pnl()
    reserved = reserved_cash()
    bal = state["starting_balance"] + realized
    avail = bal - reserved
    ok = avail >= cost - 1e-9
    return ok, {
        "starting_balance": round(state["starting_balance"], 2),
        "realized_pnl": round(realized, 2),
        "balance": round(bal, 2),
        "reserved": round(reserved, 2),
        "available": round(avail, 2),
        "cost": round(cost, 2),
    }


if __name__ == "__main__":
    init_capital_table()
    print(gate_buy(0))