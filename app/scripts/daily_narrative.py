"""Daily LLM-generated narrative for the v2 paper trader.

At end-of-day this script pulls the day's closed trades and produces a
short plain-English summary of what went right and what went wrong.

The LLM is used for NARRATION only — it does not see strategy params
that could be used to inject bad suggestions. It sees:

- The day's trades (symbol, direction, score, regime, P&L, hold time)
- A few aggregate stats (WR, avg win/loss, by-symbol breakdown)
- Current policy state (frozen, loss cap, last adapt reason)

The output is constrained to under 280 words and explicitly prompted
to be SKEPTICAL — to flag anomalies, not to celebrate wins.
"""
from __future__ import annotations
import json
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path('/home/ralph/trader-v2')
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'
RESULTS_FILE = PROJECT / 'data' / 'daily_narrative.json'

sys.path.insert(0, '/home/ralph/trader-v2/scripts')
from llm_models import model_for

OLLAMA_URL = 'http://127.0.0.1:11434/v1/chat/completions'
OLLAMA_CLOUD_MODEL = model_for('narrative')
OLLAMA_LOCAL_MODEL = 'qwen2.5:7b'  # default — runs on GPU
OLLAMA_MODEL = OLLAMA_LOCAL_MODEL
MAX_OUTPUT_WORDS = 280


def fetch_day_trades(trading_day=None):
    """Fetch all closed paper trades for a given trading day."""
    if trading_day is None:
        conn = sqlite3.connect(PAPER_DB)
        try:
            row = conn.execute(
                "SELECT DATE(timestamp_open) FROM trades "
                "WHERE mode='paper' AND timestamp_close IS NOT NULL "
                "ORDER BY timestamp_open DESC LIMIT 1"
            ).fetchone()
            trading_day = row[0] if row else datetime.now().strftime('%Y-%m-%d')
        finally:
            conn.close()

    conn = sqlite3.connect(PAPER_DB)
    try:
        rows = conn.execute(
            "SELECT t.symbol, t.option_type, t.option_strike, t.entry_price, "
            "t.exit_price, t.pnl, t.fees, t.net_pnl, "
            "t.timestamp_open, t.timestamp_close, "
            "t.exit_reason, t.entry_signal_score, "
            "s.regime_label, s.rsi_value, s.vix "
            "FROM trades t LEFT JOIN signals s ON s.trade_id = t.id "
            "WHERE t.mode='paper' AND t.timestamp_close IS NOT NULL "
            "AND DATE(t.timestamp_open) = ? "
            "ORDER BY t.timestamp_open",
            (trading_day,),
        ).fetchall()
    finally:
        conn.close()

    cols = ['symbol', 'option_type', 'option_strike', 'entry_price',
            'exit_price', 'pnl', 'fees', 'net_pnl',
            'timestamp_open', 'timestamp_close',
            'exit_reason', 'entry_signal_score',
            'regime_label', 'rsi_value', 'vix']
    result = []
    for row in rows:
        d = dict(zip(cols, row))
        try:
            t_open = datetime.fromisoformat(d['timestamp_open'])
            t_close = datetime.fromisoformat(d['timestamp_close'])
            d['hold_minutes'] = round((t_close - t_open).total_seconds() / 60, 1)
        except Exception:
            d['hold_minutes'] = 0
        result.append(d)
    return result


def aggregate(trades):
    """Compute aggregate stats for the day."""
    if not trades:
        return {'n_trades': 0}
    pnls = [t['net_pnl'] or 0 for t in trades]
    by_symbol = {}
    for t in trades:
        by_symbol.setdefault(t['symbol'], []).append(t['net_pnl'] or 0)
    return {
        'n_trades': len(trades),
        'wins': sum(1 for p in pnls if p > 0),
        'losses': sum(1 for p in pnls if p < 0),
        'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
        'gross_pnl': round(sum(pnls), 2),
        'avg_win': round(sum(p for p in pnls if p > 0) / max(1, sum(1 for p in pnls if p > 0)), 2),
        'avg_loss': round(sum(p for p in pnls if p < 0) / max(1, sum(1 for p in pnls if p < 0)), 2),
        'best_trade': round(max(pnls), 2),
        'worst_trade': round(min(pnls), 2),
        'by_symbol': {
            sym: {
                'n': len(pls),
                'pnl': round(sum(pls), 2),
                'wr': round(sum(1 for p in pls if p > 0) / len(pls), 4),
            }
            for sym, pls in by_symbol.items()
        },
        'exit_reasons': {
            r: sum(1 for t in trades if t.get('exit_reason') == r)
            for r in set(t.get('exit_reason')
                         for t in trades if t.get('exit_reason'))
        },
    }


def build_prompt(stats, trades, policy):
    """Build the LLM prompt with stats as facts (numeric, not free-text)."""
    facts = json.dumps(stats, indent=2)
    n_trades = stats['n_trades']
    if n_trades == 0:
        return (
            "There were no closed paper trades today. "
            "Write a one-sentence acknowledgment."
        )

    sample_trades = trades[:8]
    trade_summary = '\n'.join(
        f"  {t['symbol']} {t['option_type']} ${t['option_strike']} "
        f"score={t.get('entry_signal_score', 0):.2f} "
        f"regime={t.get('regime_label', '?')} "
        f"hold={t.get('hold_minutes', 0):.0f}m "
        f"pnl=${t['net_pnl']:+.2f} "
        f"exit={t.get('exit_reason', '?')}"
        for t in sample_trades
    )

    pl = policy
    policy_state = (
        f"policy.frozen={pl.get('frozen', False)}, "
        f"loss_cap=${pl.get('loss_cap_dollar_v2', 0):.0f}, "
        f"adapt_reason={pl.get('adapt_reason', 'n/a')}"
    )

    return (
        "You are a skeptical trading-floor analyst reviewing the day's results "
        "on a paper trading account. You are precise and avoid hype.\n\n"
        f"Here are the day's actual numbers (no interpretation yet):\n{facts}\n\n"
        f"First 8 trades (for context):\n{trade_summary}\n\n"
        f"Current policy state: {policy_state}\n\n"
        "Write a 200-280 word plain-English summary covering:\n"
        "1. The day's bottom line (do not bury it)\n"
        "2. What went right and on what symbol\n"
        "3. What went wrong and on what symbol\n"
        "4. Any pattern you notice (e.g. 'all losses were in 2-3pm window' or "
        "'regime was volatile and bot over-traded')\n"
        "5. One skeptical observation — something the bot might be missing or "
        "a risk the user should know about\n\n"
        "Be specific. Use the actual numbers. Do not say 'great day' if the day was bad. "
        "Do not recommend parameter changes (you do not have authority for that) — "
        "just describe what you see."
    )


def call_llm(prompt):
    """Call Ollama's local LLM. Falls back to cloud if unavailable.

    Local model is preferred (free, on-GPU). Cloud is the safety net so
    the daily narrative still gets generated when the local model errors.
    """
    import urllib.request
    last_err = None
    for model_name in (OLLAMA_LOCAL_MODEL, OLLAMA_CLOUD_MODEL):
        try:
            data = json.dumps({
                'model': model_name,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 600,
                'temperature': 0.3,
            }).encode()
            req = urllib.request.Request(
                OLLAMA_URL, data=data,
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return result['choices'][0]['message']['content'].strip()
        except Exception as e:
            last_err = e
            continue
    return f"[LLM unavailable: {type(last_err).__name__}. Rely on stats above.]"


def main():
    """Generate today's narrative. Writes to data/daily_narrative.json."""
    policy_path = PROJECT / 'data' / 'policy.json'
    policy = {}
    if policy_path.exists():
        try:
            with open(policy_path) as f:
                policy = json.load(f)
        except Exception:
            pass

    trades = fetch_day_trades()
    stats = aggregate(trades)
    print(f"Loaded {len(trades)} closed trades for day")

    if stats['n_trades'] == 0:
        result = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'trading_day': datetime.now().strftime('%Y-%m-%d'),
            'aggregate_stats': stats,
            'narrative': 'No closed paper trades today.',
            'llm_used': False,
        }
        with open(RESULTS_FILE, 'w') as f:
            json.dump(result, f, indent=2)
        print(f"No trades — wrote minimal narrative to {RESULTS_FILE}")
        return result

    prompt = build_prompt(stats, trades, policy)
    narrative = call_llm(prompt)

    words = narrative.split()
    if len(words) > MAX_OUTPUT_WORDS:
        narrative = ' '.join(words[:MAX_OUTPUT_WORDS]) + '...'

    result = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'trading_day': datetime.now().strftime('%Y-%m-%d'),
        'aggregate_stats': stats,
        'narrative': narrative,
        'llm_used': 'unavailable' not in narrative,
    }

    with open(RESULTS_FILE, 'w') as f:
        json.dump(result, f, indent=2)

    print(f"Narrative saved to {RESULTS_FILE}")
    print(f"\n{'=' * 60}\n{narrative}\n{'=' * 60}")
    return result


if __name__ == '__main__':
    main()
