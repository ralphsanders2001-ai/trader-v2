"""
Daily EOD Summary Notification

Runs at 4:10 PM ET (after shutdown cron at 4:05 PM).
Sends Ralph a plain-text end-of-day recap to Telegram:
- Total trades, wins, losses, net P&L
- vs $100/day target
- vs $250 max daily loss cap
- Open positions
- Watchlist symbols tracked

No alerts during trading hours — only end-of-day recap.
"""
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
ET = timezone(timedelta(hours=-4))
DAILY_TARGET = 100.0
DAILY_MAX_LOSS = 250.0


def fetch_today_stats():
    today = datetime.now(ET).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)

    # Today's closed trades
    trades = conn.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl) as total_pnl,
               SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
               SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as gross_loss
        FROM trades
        WHERE DATE(timestamp_open) = ?
          AND pnl IS NOT NULL
    """, [today]).fetchone()

    # Bot vs manual breakdown using placed_by field
    # 'api' = bot placed, 'user' = manual, 'system' = Robinhood-managed
    breakdown = conn.execute("""
        SELECT placed_by,
               COUNT(*) as cnt,
               SUM(pnl) as pnl_total
        FROM trades
        WHERE DATE(timestamp_open) = ?
          AND pnl IS NOT NULL
          AND placed_by IS NOT NULL
          AND placed_by != 'unknown'
        GROUP BY placed_by
    """, [today]).fetchall()

    # Open positions (no close_time = still open)
    open_positions = conn.execute("""
        SELECT symbol, option_type, option_strike, entry_price, last_poll_price
        FROM positions
        WHERE on_hold = 0
    """).fetchall()

    # Circuit breaker state
    circuit = conn.execute("""
        SELECT realized_pnl, tripped FROM circuit_breaker
        WHERE trading_day = ?
    """, [today]).fetchone()

    conn.close()

    return {
        "trades_count": trades[0] or 0,
        "wins": trades[1] or 0,
        "losses": trades[2] or 0,
        "net_pnl": trades[3] or 0.0,
        "gross_profit": trades[4] or 0.0,
        "gross_loss": trades[5] or 0.0,
        "open_positions": open_positions,
        "circuit_pnl": circuit[0] if circuit else 0.0,
        "circuit_tripped": bool(circuit[1]) if circuit else False,
        "breakdown": {row[0]: {"count": row[1], "pnl": row[2]} for row in breakdown},
    }


def format_summary(stats):
    lines = []
    lines.append(f"TRADER V2 — Daily Recap {datetime.now(ET).strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"Trades: {stats['trades_count']}  ({stats['wins']}W / {stats['losses']}L)")
    lines.append(f"Net P&L: ${stats['net_pnl']:+.2f}")

    if stats['net_pnl'] >= DAILY_TARGET:
        lines.append(f"Target hit: ${stats['net_pnl']:.2f} vs ${DAILY_TARGET:.0f} goal")
    elif stats['net_pnl'] > 0:
        lines.append(f"Below target: ${stats['net_pnl']:.2f} of ${DAILY_TARGET:.0f} goal")
    else:
        lines.append(f"Down day: ${stats['net_pnl']:.2f}")

    if stats['gross_profit'] != 0 or stats['gross_loss'] != 0:
        lines.append(f"Gross profit: ${stats['gross_profit']:+.2f}")
        lines.append(f"Gross loss: ${stats['gross_loss']:+.2f}")

    lines.append("")

    # Loss cap check
    if stats['circuit_tripped']:
        lines.append(f"CIRCUIT BREAKER TRIPPED — trading halted")
    else:
        loss_used_pct = abs(min(0, stats['net_pnl'])) / DAILY_MAX_LOSS * 100
        lines.append(f"Loss cap: ${DAILY_MAX_LOSS:.0f} ({loss_used_pct:.0f}% used)")

    lines.append("")

    # Open positions
    if stats['open_positions']:
        lines.append(f"Open positions ({len(stats['open_positions'])}):")
        for pos in stats['open_positions']:
            sym, otype, strike, entry, last = pos
            change = (last - entry) if last else 0
            lines.append(f"  {sym} {otype} ${strike} entry ${entry:.2f} last ${last:.2f} ({change:+.2f})")
    else:
        lines.append("No open positions.")

    lines.append("")

    # Bot vs manual breakdown
    bd = stats.get("breakdown", {})
    if bd:
        lines.append("Source breakdown:")
        if "api" in bd:
            lines.append(f"  Bot: {bd['api']['count']} trades, ${bd['api']['pnl']:+.2f}")
        if "user" in bd:
            lines.append(f"  Manual: {bd['user']['count']} trades, ${bd['user']['pnl']:+.2f}")
        if "system" in bd:
            lines.append(f"  System: {bd['system']['count']} trades, ${bd['system']['pnl']:+.2f}")

    return "\n".join(lines)


def main():
    stats = fetch_today_stats()
    summary = format_summary(stats)
    print(summary)

    # Save summary to a file for the notification system to pick up
    today = datetime.now(ET).strftime("%Y-%m-%d")
    summary_dir = Path("/home/ralph/trader-v2/daily-summaries")
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_file = summary_dir / f"summary-{today}.txt"
    summary_file.write_text(summary)

    # Save to NAS as well
    nas_dir = Path("/mnt/file-cabinet/trader-v2/daily-summaries")
    nas_dir.mkdir(parents=True, exist_ok=True)
    (nas_dir / f"summary-{today}.txt").write_text(summary)

    print(f"\nSaved: {summary_file}")


if __name__ == "__main__":
    main()