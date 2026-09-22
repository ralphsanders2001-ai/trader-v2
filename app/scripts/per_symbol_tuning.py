"""Per-symbol policy tuning for the v2 paper trader.

Different symbols have different behavior:
- SPY: lowest vol, tight spreads, mean-reverts
- QQQ: slightly higher vol than SPY, similar behavior
- NVDA: highest vol, widest spreads, trending

Per-symbol stats let us tune loss cap, take profit, and ML threshold
independently for each. The risk: with only ~25 trades per symbol, the
per-symbol estimates are noisy. We use shrinkage: blend per-symbol stats
with the global stats, weighted by sample size.

Output: per-symbol_tuning.json with recommended caps per symbol.
"""
from __future__ import annotations
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

PROJECT = Path('/home/ralph/trader-v2')
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'
RESULTS_FILE = PROJECT / 'data' / 'per_symbol_tuning.json'

# Hard bounds (must match policy.py)
BOUNDS = {
    'loss_cap_dollar_v2': {'min': 20.0, 'max': 50.0, 'step': 5.0},
    'take_profit_dollar': {'min': 5.0,  'max': 20.0, 'step': 1.0},
    'ml_skip_threshold':  {'min': 0.50, 'max': 0.70, 'step': 0.02},
}

# Minimum samples before per-symbol stats override global
MIN_SAMPLES_FOR_TUNING = 15


def per_symbol_stats() -> dict:
    """Compute win rate, mean P&L, and a per-symbol realized vol proxy."""
    conn = sqlite3.connect(PAPER_DB)
    try:
        rows = conn.execute('''
            SELECT symbol, pnl FROM trades
            WHERE timestamp_close IS NOT NULL
              AND pnl IS NOT NULL
              AND mode = 'paper'
            ORDER BY id
        ''').fetchall()
    finally:
        conn.close()

    by_symbol: dict = {}
    for sym, pnl in rows:
        by_symbol.setdefault(sym, []).append(float(pnl))

    stats = {}
    for sym, pnls in by_symbol.items():
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / max(1, n - 1)
        std = math.sqrt(var)
        stats[sym] = {
            'n_trades': n,
            'win_rate': round(len(wins) / n, 4) if n else 0,
            'avg_win':  round(sum(wins) / len(wins), 2) if wins else 0,
            'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0,
            'mean_pnl': round(mean, 2),
            'std_pnl':  round(std, 2),
            'total_pnl': round(sum(pnls), 2),
        }
    return stats


def shrink(global_val: float, symbol_val: float, n: int, prior_n: int = 30) -> float:
    """Bayesian shrinkage — blend symbol value with global value.

    With n small, weight symbol_val less. With n large, weight it more.
    prior_n is the "effective" global sample size.
    """
    w = n / (n + prior_n)
    return w * symbol_val + (1 - w) * global_val


def propose_caps(sym: str, sym_stats: dict, global_stats: dict) -> dict:
    """Propose per-symbol loss cap and take profit based on realized stats.

    Logic:
    - If symbol has higher realized vol → smaller loss cap (catches quick drops)
    - If symbol has higher avg_win/avg_loss ratio → bigger take profit
    - If symbol WR < 35% → tighten ML threshold (skip more trades)
    """
    n = sym_stats['n_trades']
    g_wr = global_stats['win_rate']
    g_avg_loss = abs(global_stats['avg_loss'])
    g_avg_win = global_stats['avg_win']

    if n < MIN_SAMPLES_FOR_TUNING:
        return {
            'tuned': False,
            'reason': f'insufficient_samples ({n} < {MIN_SAMPLES_FOR_TUNING})',
            'use_global': True,
        }

    # Blend symbol WR with global (shrink toward global)
    s_wr = shrink(g_wr, sym_stats['win_rate'], n)
    s_avg_loss = shrink(g_avg_loss, abs(sym_stats['avg_loss']), n)
    s_avg_win = shrink(g_avg_win, sym_stats['avg_win'], n)
    s_std = shrink(global_stats['std_pnl'], sym_stats['std_pnl'], n)

    # Loss cap: clamp symbol's avg_loss at BOUNDS['loss_cap_dollar_v2']['max']
    # If avg_loss is small (tight symbol), we can be more aggressive
    loss_cap = max(BOUNDS['loss_cap_dollar_v2']['min'],
                   min(BOUNDS['loss_cap_dollar_v2']['max'], s_avg_loss * 1.2))
    # Snap to step
    step = BOUNDS['loss_cap_dollar_v2']['step']
    loss_cap = round(loss_cap / step) * step

    # Take profit: aim for 1.5x avg_loss for healthy R:R
    target_rr = 1.5
    if s_avg_loss > 0:
        take_profit = max(BOUNDS['take_profit_dollar']['min'],
                          min(BOUNDS['take_profit_dollar']['max'], s_avg_loss * target_rr))
        step_tp = BOUNDS['take_profit_dollar']['step']
        take_profit = round(take_profit / step_tp) * step_tp
    else:
        take_profit = 10.0  # safe default

    # ML threshold: tighten if WR is low
    ml_thresh = 0.55  # default
    if s_wr < 0.35:
        ml_thresh = 0.60
    elif s_wr < 0.45:
        ml_thresh = 0.57
    elif s_wr > 0.55:
        ml_thresh = 0.53

    return {
        'tuned': True,
        'n_samples': n,
        'realized_wr': round(s_wr, 4),
        'realized_avg_loss': round(s_avg_loss, 2),
        'realized_avg_win': round(s_avg_win, 2),
        'recommended_loss_cap_dollar': loss_cap,
        'recommended_take_profit_dollar': take_profit,
        'recommended_ml_skip_threshold': ml_thresh,
        'rationale': (
            f"WR {s_wr:.1%}, avg_loss ${s_avg_loss:.2f}, avg_win ${s_avg_win:.2f}"
        ),
    }


def main() -> dict:
    print('=' * 60)
    print('PER-SYMBOL POLICY TUNING')
    print('=' * 60)

    by_symbol = per_symbol_stats()
    if not by_symbol:
        print('No trades found')
        return {}

    # Compute global stats for shrinkage
    all_pnls = []
    for stats in by_symbol.values():
        # Recompute from stored raw — we don't have raw here, but we have mean/std/n
        # Approximation: just use the per-symbol mean to derive global
        all_pnls.extend([stats['mean_pnl']] * stats['n_trades'])
    n_global = sum(s['n_trades'] for s in by_symbol.values())
    total_pnl = sum(s['total_pnl'] for s in by_symbol.values())
    global_mean = total_pnl / n_global if n_global else 0
    # Global win rate (sum of wins / total)
    total_wins = sum(s['n_trades'] * s['win_rate'] for s in by_symbol.values())
    global_wr = total_wins / n_global if n_global else 0
    # Global avg loss and win — approximate from per-symbol data
    all_wins = [s['avg_win'] for s in by_symbol.values() if s['avg_win'] > 0]
    all_losses = [abs(s['avg_loss']) for s in by_symbol.values() if s['avg_loss'] < 0]
    global_avg_win = sum(all_wins) / len(all_wins) if all_wins else 10.0
    global_avg_loss = sum(all_losses) / len(all_losses) if all_losses else 30.0
    # Global std: weighted average of per-symbol std
    global_std = sum(s['std_pnl'] * s['n_trades'] for s in by_symbol.values()) / n_global if n_global else 50.0

    global_stats = {
        'win_rate': global_wr,
        'avg_win': global_avg_win,
        'avg_loss': global_avg_loss,
        'mean_pnl': global_mean,
        'std_pnl': global_std,
    }

    print(f'\nGlobal stats: WR={global_wr:.1%}, avg_win=${global_avg_win:.2f}, avg_loss=${global_avg_loss:.2f}')

    tuning = {}
    for sym, stats in by_symbol.items():
        print(f'\n--- {sym} ({stats["n_trades"]} trades, WR {stats["win_rate"]:.1%}, total ${stats["total_pnl"]:+.2f}) ---')
        proposal = propose_caps(sym, stats, global_stats)
        if proposal['tuned']:
            print(f'  → Loss cap:  ${proposal["recommended_loss_cap_dollar"]:.0f}')
            print(f'  → Take prof: ${proposal["recommended_take_profit_dollar"]:.0f}')
            print(f'  → ML gate:   {proposal["recommended_ml_skip_threshold"]:.2f}')
            print(f'  Rationale: {proposal["rationale"]}')
        else:
            print(f'  → Use global: {proposal["reason"]}')
        tuning[sym] = proposal

    output = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'global_stats': {k: round(v, 4) for k, v in global_stats.items()},
        'per_symbol': tuning,
        'min_samples_threshold': MIN_SAMPLES_FOR_TUNING,
        'note': 'Apply these caps via the policy_overlay.py file when ready. Do not go live until per_symbol stats are stable.',
    }

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'\nSaved to {RESULTS_FILE}')
    return output


if __name__ == '__main__':
    main()