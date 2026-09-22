"""
Autonomous trade runner: scans for signals and enters best ones if account allows.
Run via cron at market open or on demand.
"""
import sys, os, json
from datetime import date, datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import init_db, get_db
from account import AccountManager
from signals import momentum_pullback_signal
from execution import ExecutionEngine
from performance import PerformanceTracker
from notify import notify
import config
from universe import get_scan_symbols

LOSS_SKIP_THRESHOLD = 0.0    # Skip symbol if any closed trade had a loss

def _get_recent_loss_symbols(mode: str) -> set:
    """Return symbols to skip because recent closed trade was a loss.

    Cooldown is limited to COOLDOWN_MINUTES — after that, the symbol is fair
    game again. Gives the symbol a chance to re-enter when conditions change.
    """
    today = date.today().isoformat()
    now = datetime.now(timezone.utc)
    cooldown = timedelta(minutes=config.LOSS_COOLDOWN_MINUTES)

    skipped = set()
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT symbol, pnl, exit_time
            FROM trades
            WHERE mode=? AND status='closed' AND trade_date=? AND pnl IS NOT NULL
        """, (mode, today)).fetchall()

    # Group by symbol, keep only losses that are still in cooldown
    for symbol, pnl, exit_time_str in rows:
        if pnl > LOSS_SKIP_THRESHOLD:
            continue
        if not exit_time_str:
            skipped.add(symbol)
            continue
        try:
            exit_time = datetime.fromisoformat(exit_time_str.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            skipped.add(symbol)
            continue
        if (now - exit_time) < cooldown:
            skipped.add(symbol)

    return skipped

def can_trade_today(state) -> str:
    """Returns empty string if trading allowed, otherwise reason."""
    if state.mode not in ("paper", "live"):
        return f"mode is {state.mode}"
    if state.buy_count >= config.MAX_BUYS_PER_DAY:
        return f"max {config.MAX_BUYS_PER_DAY} buys/day reached"
    pt = PerformanceTracker()
    summary = pt.daily_summary(state.mode)
    if summary and summary["net_pnl"] <= -config.DAILY_LOSS_LIMIT:
        return f"daily loss limit {config.DAILY_LOSS_LIMIT} reached"
    if summary and summary["net_pnl"] >= config.DAILY_PROFIT_GOAL:
        return f"daily profit goal {config.DAILY_PROFIT_GOAL} reached"
    return ""

def run(max_entries: int = 1, mode: str = "paper"):
    init_db()
    account = AccountManager()
    account.check_and_reset_counts(mode=mode)
    state = account.get_state(mode=mode)

    # In live mode, refresh BP from Robinhood BEFORE the scan — so that the
    # per-trade BP check doesn't reject good trades on stale local data.
    if mode == "live":
        try:
            import sys
            sys.path.insert(0, os.path.dirname(__file__))
            from broker import LiveBroker
            broker = LiveBroker()
            port = broker.get_portfolio()
            if 'error' not in port:
                account.update_state(
                    mode=mode,
                    buying_power=port.get('buying_power', state.buying_power),
                    equity=port.get('equity', state.equity),
                    cash=port.get('cash', state.cash),
                )
                state = account.get_state(mode=mode)
                print(f"[LIVE] Refreshed BP from Robinhood: ${port.get('buying_power', 0):.2f}")
        except Exception as e:
            print(f"[WARN] Live BP refresh failed: {e}")

    block_reason = can_trade_today(state)
    if block_reason:
        print(f"[{mode.upper()}] Trading blocked: {block_reason}")
        return

    # Build skip list from recent losses
    skip_symbols = _get_recent_loss_symbols(mode)
    if skip_symbols:
        print(f"[{mode.upper()}] Skipping loss-tainted symbols: {skip_symbols}")

    symbols = get_scan_symbols()
    candidates = []
    all_evaluated = []

    # Scan both calls and puts in live mode too — pick the side with the
    # higher score per symbol. Filters out unaffordable strikes later.
    sides = ("call", "put")

    for symbol in symbols:
        for side in sides:
            sig = momentum_pullback_signal(symbol, side=side)
            all_evaluated.append(sig)

            # Skip if symbol is on loss cooldown (DB)
            if symbol in skip_symbols:
                print(f"  SKIP {symbol} {side}: loss cooldown (recent loss >= ${abs(LOSS_SKIP_THRESHOLD):.0f})")
                continue

            if sig.action == "buy":
                candidates.append(sig)

    # Sort candidates by score descending
    candidates.sort(key=lambda s: -s.score)

    # When both a call and put scored buy for the same symbol, keep only the
    # higher-scoring side — avoids straddling the same name in opposite
    # directions on the same signal.
    by_symbol = {}
    for sig in candidates:
        cur = by_symbol.get(sig.symbol)
        if cur is None or sig.score > cur.score:
            by_symbol[sig.symbol] = sig
    candidates = list(by_symbol.values())
    candidates.sort(key=lambda s: -s.score)

    # Drop signals whose option is unaffordable for this account size (ATM
    # puts on liquid names routinely cost more than BP allows).
    bp_before = len(candidates)
    candidates = [s for s in candidates if s.option_premium * 100 <= state.buying_power]
    if len(candidates) < bp_before:
        print(f"  [{mode.upper()}] Filtered {bp_before} -> {len(candidates)} candidates by buying power")

    # Skip symbols that already have open positions (avoid duplicate orders in both paper and live)
    open_symbols = set()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE mode=? AND status IN ('open','pending_close')",
            (mode,)
        ).fetchall()
        for (sym,) in rows:
            open_symbols.add(sym)
    before = len(candidates)
    candidates = [c for c in candidates if c.symbol not in open_symbols]
    if open_symbols:
        print(f"  [{mode.upper()}] Skipping already-open symbols: {open_symbols}")
        print(f"  Filtered {before} -> {len(candidates)} candidates")

    print(f"\n[{mode.upper()}] SCAN COMPLETE — {len(all_evaluated)} candidates evaluated, {len(candidates)} signals")
    for sig in all_evaluated:
        if sig.action != "buy":
            print(f"  SKIP {sig.symbol} {sig.side}: {sig.reason} (score={sig.score})")
        elif sig.symbol in skip_symbols:
            print(f"  SKIP {sig.symbol} {sig.side}: loss cooldown (score={sig.score})")

    if not candidates:
        print(f"[{mode.upper()}] No actionable candidates")
        return

    engine = ExecutionEngine(mode=mode)
    entries = 0
    for sig in candidates[:max_entries]:
        if state.buy_count + entries >= config.MAX_BUYS_PER_DAY:
            print(f"[{mode.upper()}] Max trades per day reached, no more entries")
            break
        if state.buying_power < sig.option_premium * 100:
            print(f"[{mode.upper()}] Insufficient buying power for {sig.symbol} {sig.side} {sig.strike}")
            continue
        result = engine.enter(sig, quantity=config.MAX_CONTRACTS_PER_TRADE)

        print(f"[{mode.upper()}] {result.message}")
        if result.success:
            entries += 1
            print(
                f"ALERT: ENTERED {sig.symbol} {sig.side} "
                f"strike={sig.strike} premium={sig.option_premium:.2f} "
                f"score={sig.score} trade_id={result.trade_id} mode={mode}"
            )
            notify(
                f"TRADE ENTERED: {sig.symbol}",
                f"{sig.side.upper()} {sig.strike} exp {sig.expiry}\n"
                f"Premium: ${sig.option_premium:.2f} | Score: {sig.score}\n"
                f"Mode: {mode}",
                priority="high",
                tags=["chart_with_upwards_trend"]
            )

    if entries == 0:
        print(f"[{mode.upper()}] No trades entered")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="paper")
    parser.add_argument("--max-entries", type=int, default=1)
    args = parser.parse_args()
    run(max_entries=args.max_entries, mode=args.mode)
