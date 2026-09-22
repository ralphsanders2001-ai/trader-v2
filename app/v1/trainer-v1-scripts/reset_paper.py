"""
Reset paper account state for a clean start.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_db

def reset_paper():
    init_db()
    with get_db() as conn:
        conn.execute("DELETE FROM trades WHERE mode='paper'")
        conn.execute("DELETE FROM daily_summary WHERE mode='paper'")
        conn.execute(
            "UPDATE account_state SET cash=1000.0, equity=1000.0, buying_power=1000.0, buy_count=0, sell_count=0, count_reset_date=date('now'), updated_at=datetime('now') WHERE mode='paper'"
        )
        conn.commit()
    print("Paper account reset: cash=1000, buying_power=1000, buy_count=0, trades cleared")

if __name__ == "__main__":
    reset_paper()
