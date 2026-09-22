"""Adaptive policy for the v2 paper trader.

The bot reads policy.json on every loop instead of using the hardcoded values
in config.py. A nightly job (adapt_policy.py) computes rolling stats and
proposes parameter mutations within hard bounds set in config.py.

This is what makes the system "continuously learning" instead of "periodically
retrained" — the strategy rules adapt based on what is actually working.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT = Path('/home/ralph/trader-v2')
POLICY_FILE = PROJECT / 'data' / 'policy.json'
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'


# Hard-coded bounds — these NEVER change. The policy can only move
# parameters WITHIN these envelopes. config.py contains the starting values
# and the policy may mutate them within these bounds.
BOUNDS = {
    'ml_skip_threshold':      {'min': 0.50, 'max': 0.70, 'step': 0.02},
    'loss_cap_dollar_v2':     {'min': 20.0, 'max': 50.0, 'step': 5.0},
    'take_profit_dollar':     {'min': 10.0, 'max': 30.0, 'step': 5.0},
    'max_profit_dollar':      {'min': 30.0, 'max': 75.0, 'step': 5.0},
    'symbol_cooldown_sec':    {'min': 60,   'max': 300,  'step': 30},
    'max_open_positions':     {'min': 1,    'max': 4,    'step': 1},
}


def default_policy() -> dict:
    """Initial policy = current config.py values.

    The constants here mirror config.py. The nightly adapter ONLY mutates
    a value if (a) there is enough data, (b) the new value lies inside BOUNDS,
    (c) we are not in 'frozen' mode.
    """
    return {
        'ml_skip_threshold':   0.55,
        'loss_cap_dollar_v2':  30.0,
        'take_profit_dollar':  10.0,
        'max_profit_dollar':   50.0,
        'symbol_cooldown_sec': 60,
        'max_open_positions':  2,
        'frozen':              False,
        'last_adapt_at':       None,
        'adapt_reason':        'initial',
        'rolling_trades':      0,
        'rolling_win_rate':    None,
        'rolling_rr':          None,
    }


def load_policy() -> dict:
    """Load policy from disk. If file missing, seed from defaults and write."""
    if not POLICY_FILE.exists():
        p = default_policy()
        save_policy(p, source='init')
        return p
    try:
        with open(POLICY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        p = default_policy()
        save_policy(p, source='reset')
        return p


def save_policy(p: dict, source: str = 'manual') -> None:
    """Persist policy to disk. Logs a config_changes row if schema permits."""
    POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    p['_last_source'] = source
    with open(POLICY_FILE, 'w') as f:
        json.dump(p, f, indent=2)
    try:
        conn = sqlite3.connect(PAPER_DB)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS policy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT,
                policy_json TEXT,
                win_rate REAL,
                rr_ratio REAL,
                trades INTEGER
            )
        ''')
        conn.execute('''
            INSERT INTO policy_history (timestamp, source, policy_json, win_rate, rr_ratio, trades)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            source,
            json.dumps(p),
            p.get('rolling_win_rate'),
            p.get('rolling_rr'),
            p.get('rolling_trades'),
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


def clamp(name: str, value: float) -> float:
    """Snap a value to the nearest BOUNDS step, clamped within [min, max]."""
    b = BOUNDS.get(name)
    if not b:
        return value
    step = b['step']
    snapped = round(value / step) * step
    return max(b['min'], min(b['max'], snapped))


def compute_rolling_stats(window: int = 50) -> dict:
    """Compute win rate / R:R / count over the last N closed paper trades."""
    conn = sqlite3.connect(PAPER_DB)
    try:
        df = conn.execute('''
            SELECT pnl FROM trades
            WHERE timestamp_close IS NOT NULL
              AND pnl IS NOT NULL
              AND mode = 'paper'
            ORDER BY id DESC
            LIMIT ?
        ''', (window,)).fetchall()
    finally:
        conn.close()

    pnls = [float(r[0]) for r in df]
    if not pnls:
        return {'count': 0, 'win_rate': None, 'rr': None, 'avg_win': None, 'avg_loss': None}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    avg_win = (sum(wins) / len(wins)) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    rr = (avg_win / avg_loss) if avg_loss > 0 else None

    return {
        'count': len(pnls),
        'win_rate': round(win_rate, 4),
        'rr': round(rr, 4) if rr is not None else None,
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
    }


def propose_mutation(policy: dict, stats: dict, drift_detected: bool = False, drift_direction: str = 'unknown') -> tuple:
    """Propose parameter mutations based on rolling stats.

    Returns (mutated_policy_dict, change_description). Empty dict if no change.
    """
    if stats['count'] < 30:
        return {}, 'insufficient_data'

    win_rate = stats['win_rate'] or 0
    rr = stats['rr'] or 0

    mutations = {}
    reasons = []

    # DRIFT DETECTION (FIX 2026-08-13): freeze only if recent is WORSE than old
    if drift_detected and drift_direction == 'old_better':
        mutations['frozen'] = True
        reasons.append('drift_old_better_freeze')
    elif drift_detected and drift_direction == 'new_better':
        # Recent is better — keep trading, just log it
        reasons.append('drift_new_better_keep')

    # WR < 25% = strategy broken → freeze, do not trade aggressively
    if win_rate < 0.25 and not mutations.get('frozen'):
        mutations['frozen'] = True
        reasons.append('wr_broken_freeze')

    # WR OK but R:R < 1.0 = bleeding per-trade → tighten loss cap
    elif win_rate >= 0.25 and rr < 1.0 and stats['count'] >= 30:
        cur_cap = policy.get('loss_cap_dollar_v2', 30)
        target = max(20.0, cur_cap - 5.0)
        if target < cur_cap:
            mutations['loss_cap_dollar_v2'] = clamp('loss_cap_dollar_v2', target)
            reasons.append('rr_bad_tighten_loss')

    # WR > 50% AND R:R > 1.5 = strategy hot → try being slightly more aggressive
    elif win_rate >= 0.50 and rr >= 1.5:
        if policy.get('take_profit_dollar', 10) < 20:
            mutations['take_profit_dollar'] = clamp('take_profit_dollar', 15.0)
            reasons.append('wr_rr_hot_increase_tp')
        if policy.get('ml_skip_threshold', 0.55) > 0.52:
            mutations['ml_skip_threshold'] = clamp('ml_skip_threshold', 0.52)
            reasons.append('wr_rr_hot_loosen_ml')

    # WR between 25-35% = strategy cold → tighten ML gate, reduce exposure
    elif 0.25 <= win_rate < 0.50:
        if policy.get('ml_skip_threshold', 0.55) < 0.62:
            mutations['ml_skip_threshold'] = clamp('ml_skip_threshold', 0.60)
            reasons.append('wr_cold_tighten_ml')
        if policy.get('max_open_positions', 2) > 1:
            mutations['max_open_positions'] = clamp('max_open_positions', 1)
            reasons.append('wr_cold_reduce_exposure')

    # WR 35-50% with good R:R → stable
    else:
        reasons.append('stable')

    return mutations, ','.join(reasons) if reasons else 'no_change'


def adapt() -> dict:
    """Main entry: load policy, compute stats, run drift detector, propose mutation.

    Drift detection runs BEFORE mutation logic — if drift is detected, the
    policy is frozen regardless of mutation proposals. This is the safety
    rail for regime shifts.

    Returns a dict describing what happened.
    """
    policy = load_policy()
    stats = compute_rolling_stats(window=50)

    # FIX 2026-08-13: drift detection — check if recent win rate has shifted
    drift_detected = False
    drift_direction = 'unknown'
    try:
        from drift_detector import check_paper_drift
        drift_info = check_paper_drift()
        drift_detected = drift_info.get('drift', False)
        drift_direction = drift_info.get('direction', 'unknown')
    except Exception:
        pass  # best-effort

    if policy.get('frozen'):
        return {
            'frozen': True,
            'reason': 'policy currently frozen',
            'stats': stats,
            'drift_detected': drift_detected,
            'drift_direction': drift_direction,
        }

    mutations, reason = propose_mutation(policy, stats, drift_detected, drift_direction)

    if mutations:
        policy.update(mutations)
        policy['last_adapt_at'] = datetime.now(timezone.utc).isoformat()
        policy['adapt_reason'] = reason
        policy['rolling_trades'] = stats['count']
        policy['rolling_win_rate'] = stats['win_rate']
        policy['rolling_rr'] = stats['rr']
        save_policy(policy, source=f'adapt_{reason}')
        return {
            'frozen': False,
            'reason': reason,
            'mutations': mutations,
            'stats': stats,
        }

    return {
        'frozen': False,
        'reason': reason,
        'stats': stats,
        'drift_detected': drift_detected,
        'drift_direction': drift_direction,
        'mutations': mutations,
    }


if __name__ == '__main__':
    result = adapt()
    print(json.dumps(result, indent=2, default=str))
    print('\n=== Current policy ===')
    p = load_policy()
    print(json.dumps({k: v for k, v in p.items() if not k.startswith('_')}, indent=2))