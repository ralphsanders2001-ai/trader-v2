"""Go-live gate decision for the v2 paper trader.

This is the safety gate that answers: "Has paper trading demonstrated enough
evidence that we can safely trade real money?"

Inputs from previous analyses:
- deflated_sharpe.json: p-value for the realized Sharpe ratio
- per_symbol_tuning.json: per-symbol sample sizes
- walkforward_results.json: walk-forward AUC

Gate criteria — ALL must pass:
1. lifetime_deflated_sharpe_p < 0.10 (some statistical evidence)
2. recent_30_deflated_sharpe_p < 0.15 (recent performance isn't worse)
3. per_symbol_tuning: SPY has ≥ 30 samples, QQQ has ≥ 30 samples
4. lifetime trades ≥ 100 (sufficient data)
5. lifetime realized Sharpe is positive
6. No drift detected in the last 7 days (strategy still working)

If any criterion fails, the gate stays CLOSED and we keep trading paper.

Output:
- go_live_gate.json with verdict and per-criterion status
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path('/home/ralph/trader-v2')
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'
RESULTS_FILE = PROJECT / 'data' / 'go_live_gate.json'


def load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def check_criteria() -> dict:
    """Evaluate all gate criteria. Returns verdict + per-criterion breakdown."""
    criteria = []

    # 1. Lifetime deflated Sharpe p-value
    dsr = load_json(PROJECT / 'data' / 'deflated_sharpe.json')
    lifetime = next((w for w in dsr.get('windows', []) if w['label'] == 'lifetime'), {})
    lifetime_p = lifetime.get('deflated_sharpe_p_value')
    if lifetime_p is None:
        # Negative Sharpe — fail
        criteria.append({
            'criterion': 'lifetime_deflated_sharpe_p_lt_0.10',
            'pass': False,
            'value': None,
            'note': 'lifetime Sharpe is not positive',
        })
    else:
        criteria.append({
            'criterion': 'lifetime_deflated_sharpe_p_lt_0.10',
            'pass': lifetime_p < 0.10,
            'value': round(lifetime_p, 4),
            'note': f'lifetime p-value = {lifetime_p:.4f}',
        })

    # 2. Recent 30 DSR p-value
    recent_30 = next((w for w in dsr.get('windows', []) if w['label'] == 'recent_30'), {})
    recent_30_p = recent_30.get('deflated_sharpe_p_value')
    if recent_30_p is None:
        criteria.append({
            'criterion': 'recent_30_deflated_sharpe_p_lt_0.15',
            'pass': False,
            'value': None,
            'note': f'recent_30 Sharpe not positive: {recent_30.get("mean_pnl", 0):+.2f}',
        })
    else:
        criteria.append({
            'criterion': 'recent_30_deflated_sharpe_p_lt_0.15',
            'pass': recent_30_p < 0.15,
            'value': round(recent_30_p, 4),
            'note': f'recent_30 p-value = {recent_30_p:.4f}',
        })

    # 3. Per-symbol sample sizes
    per_symbol = load_json(PROJECT / 'data' / 'per_symbol_tuning.json')
    spy = per_symbol.get('per_symbol', {}).get('SPY', {})
    qqq = per_symbol.get('per_symbol', {}).get('QQQ', {})
    criteria.append({
        'criterion': 'SPY_n_trades_ge_30',
        'pass': spy.get('n_samples', 0) >= 30,
        'value': spy.get('n_samples', 0),
        'note': f'SPY has {spy.get("n_samples", 0)} samples (need 30)',
    })
    criteria.append({
        'criterion': 'QQQ_n_trades_ge_30',
        'pass': qqq.get('n_samples', 0) >= 30,
        'value': qqq.get('n_samples', 0),
        'note': f'QQQ has {qqq.get("n_samples", 0)} samples (need 30)',
    })

    # 4. Total trades ≥ 100
    n_trades = lifetime.get('n_trades', 0)
    criteria.append({
        'criterion': 'lifetime_n_trades_ge_100',
        'pass': n_trades >= 100,
        'value': n_trades,
        'note': f'{n_trades} trades (need 100)',
    })

    # 5. Lifetime realized Sharpe positive
    lifetime_sr = lifetime.get('sharpe_per_trade', 0) or 0
    criteria.append({
        'criterion': 'lifetime_sharpe_positive',
        'pass': lifetime_sr > 0,
        'value': round(lifetime_sr, 4),
        'note': f'lifetime Sharpe = {lifetime_sr:+.4f}',
    })

    # 6. No drift in last 7 days
    policy = load_json(PROJECT / 'data' / 'policy.json')
    frozen = policy.get('frozen', False)
    adapt_reason = policy.get('adapt_reason', '')
    criteria.append({
        'criterion': 'no_drift_in_last_7_days',
        'pass': not frozen and 'drift_old_better' not in adapt_reason,
        'value': adapt_reason,
        'note': f'policy.frozen={frozen}, last reason={adapt_reason}',
    })

    return criteria


def main() -> dict:
    print('=' * 60)
    print('GO-LIVE GATE EVALUATION')
    print('=' * 60)

    criteria = check_criteria()
    n_pass = sum(1 for c in criteria if c['pass'])
    n_total = len(criteria)

    print(f'\n{n_pass}/{n_total} criteria passing\n')
    for c in criteria:
        status = '✓ PASS' if c['pass'] else '✗ FAIL'
        print(f'  {status}  {c["criterion"]}')
        print(f'          {c["note"]}')

    all_pass = (n_pass == n_total)
    n_missing = n_total - n_pass

    if all_pass:
        verdict = 'READY_FOR_LIVE'
        reading = 'All criteria pass. Paper strategy has demonstrated statistically significant edge.'
    elif n_missing == 1:
        verdict = 'CLOSE_TO_READY'
        reading = f'{n_missing} criterion missing. Keep trading paper and re-check after more data.'
    else:
        verdict = 'NOT_READY'
        reading = f'{n_missing} criteria missing. Keep tuning strategy and trading paper.'

    print(f'\n=== VERDICT: {verdict} ===')
    print(reading)

    output = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'criteria': criteria,
        'n_pass': n_pass,
        'n_total': n_total,
        'verdict': verdict,
        'reading': reading,
        'criteria_summary': [c['criterion'] for c in criteria],
    }

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'\nSaved to {RESULTS_FILE}')
    return output


if __name__ == '__main__':
    main()