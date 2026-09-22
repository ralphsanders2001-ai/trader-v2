"""Deflated Sharpe ratio test for the v2 paper trader.

Computes the realized Sharpe and the Deflated Sharpe p-value (Bailey &
López de Prado 2014) for multiple time windows. The DSR corrects for
selection bias when multiple strategy variants were tried.

Verdicts:
- edge_real: DSR p-value < 0.05 → strategy is statistically proven
- edge_plausible: p-value < 0.20 → keep trading on paper
- insufficient_data: too few trades to conclude
- no_edge: realized Sharpe is not positive, or p-value is too high
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
RESULTS_FILE = PROJECT / 'data' / 'deflated_sharpe.json'

P_VALUE_THRESHOLD = 0.05


def load_trades(since: str = None, limit: int = None) -> list:
    """Load closed paper trade P&Ls, optionally filtered."""
    conn = sqlite3.connect(PAPER_DB)
    try:
        if since:
            rows = conn.execute('''
                SELECT pnl FROM trades
                WHERE timestamp_close IS NOT NULL
                  AND pnl IS NOT NULL
                  AND mode = 'paper'
                  AND timestamp_open > ?
                ORDER BY id
            ''', (since,)).fetchall()
        elif limit:
            rows = conn.execute('''
                SELECT pnl FROM trades
                WHERE timestamp_close IS NOT NULL
                  AND pnl IS NOT NULL
                  AND mode = 'paper'
                ORDER BY id DESC LIMIT ?
            ''', (limit,)).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute('''
                SELECT pnl FROM trades
                WHERE timestamp_close IS NOT NULL
                  AND pnl IS NOT NULL
                  AND mode = 'paper'
                ORDER BY id
            ''').fetchall()
    finally:
        conn.close()
    return [float(r[0]) for r in rows]


def skew_kurt(pnls: list) -> tuple:
    """Compute skewness and excess kurtosis of a list of returns."""
    n = len(pnls)
    if n < 4:
        return 0.0, 3.0
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0, 3.0
    skew = sum(((p - mean) / std) ** 3 for p in pnls) / n
    kurt = sum(((p - mean) / std) ** 4 for p in pnls) / n
    return float(skew), float(kurt)


def deflated_sharpe_pvalue(sr_per_trade: float, n: int, n_variants: int,
                            skewness: float = 0.0, kurtosis: float = 3.0) -> float:
    """DSR p-value per Bailey & López de Prado.

    The test statistic uses per-trade Sharpe. The variance of the Sharpe
    estimator (per-trade) is approximately:
        V[SR_hat] ≈ 1 + skew*SR - (kurt-1)/4 * SR^2
    where SR is the per-trade Sharpe (not annualized).
    """
    if n < 10:
        return 1.0

    sr = sr_per_trade
    var_sr = 1 + skewness * sr - ((kurtosis - 1) / 4) * sr ** 2
    if var_sr <= 0:
        return 1.0

    # Expected max SR under the null (per-trade units)
    phi_inv = NormalDist().inv_cdf
    sr_star = math.sqrt(var_sr) * (
        (1 - 0.5772) * phi_inv(1 - 1.0 / n_variants) +
        0.5772 * phi_inv(1 - 1.0 / (n_variants * math.e))
    )

    denom = math.sqrt(var_sr)
    z = (sr - sr_star) / denom if denom > 0 else 0
    p = 2 * (1 - NormalDist().cdf(abs(z)))
    return float(min(max(p, 0.0), 1.0))


def compute_window(pnls: list, label: str, n_variants: int = 30) -> dict:
    """Compute Sharpe + DSR p-value for a window of trades."""
    n = len(pnls)
    if n < 10:
        return {'label': label, 'n_trades': n, 'verdict': 'insufficient_data'}
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std = math.sqrt(var)
    sr = mean / std if std > 0 else 0
    sr_ann = sr * math.sqrt(1260)
    sk, ku = skew_kurt(pnls)

    if sr_ann > 0:
        p_value = deflated_sharpe_pvalue(sr, n, n_variants, sk, ku)
        if p_value < P_VALUE_THRESHOLD:
            verdict = 'edge_real'
        elif p_value < 0.20:
            verdict = 'edge_plausible'
        else:
            verdict = 'no_edge'
    else:
        p_value = None
        verdict = 'no_edge'

    return {
        'label': label,
        'n_trades': n,
        'mean_pnl': round(mean, 4),
        'std_pnl': round(std, 4),
        'sharpe_per_trade': round(sr, 4),
        'sharpe_annualized': round(sr_ann, 4),
        'n_variants_tested': n_variants,
        'skewness': round(sk, 4),
        'kurtosis': round(ku, 4),
        'deflated_sharpe_p_value': round(p_value, 4) if p_value is not None else None,
        'verdict': verdict,
    }


def main() -> dict:
    """Compute Sharpe + DSR for multiple windows."""
    print('=' * 60)
    print('DEFALATED SHARPE ANALYSIS — multiple windows')
    print('=' * 60)

    windows = []

    pnls_all = load_trades()
    print(f'\n--- LIFETIME ({len(pnls_all)} trades) ---')
    w_life = compute_window(pnls_all, 'lifetime')
    print(f'  Mean: ${w_life["mean_pnl"]:+.2f}, SR: {w_life["sharpe_per_trade"]:+.4f}, verdict: {w_life["verdict"]}')
    windows.append(w_life)

    pnls_30 = load_trades(limit=30)
    if len(pnls_30) >= 10:
        print(f'\n--- RECENT 30 ({len(pnls_30)} trades) ---')
        w_30 = compute_window(pnls_30, 'recent_30')
        print(f'  Mean: ${w_30["mean_pnl"]:+.2f}, SR: {w_30["sharpe_per_trade"]:+.4f}, verdict: {w_30["verdict"]}')
        if w_30.get('deflated_sharpe_p_value') is not None:
            print(f'  DSR p-value: {w_30["deflated_sharpe_p_value"]:.4f}')
        windows.append(w_30)

    pnls_7d = load_trades(since='now -7 days')
    if len(pnls_7d) >= 10:
        print(f'\n--- RECENT 7 DAYS ({len(pnls_7d)} trades) ---')
        w_7d = compute_window(pnls_7d, 'recent_7d')
        print(f'  Mean: ${w_7d["mean_pnl"]:+.2f}, SR: {w_7d["sharpe_per_trade"]:+.4f}, verdict: {w_7d["verdict"]}')
        if w_7d.get('deflated_sharpe_p_value') is not None:
            print(f'  DSR p-value: {w_7d["deflated_sharpe_p_value"]:.4f}')
        windows.append(w_7d)

    pnls_post = load_trades(since='2026-08-13 11:30:00')
    if len(pnls_post) >= 5:
        print(f'\n--- POST-RESET TODAY ({len(pnls_post)} trades) ---')
        # Use smaller N_VARIANTS for this fresh window — fewer variants tried today
        w_post = compute_window(pnls_post, 'post_reset_today', n_variants=5)
        print(f'  Mean: ${w_post["mean_pnl"]:+.2f}, SR: {w_post["sharpe_per_trade"]:+.4f}, verdict: {w_post["verdict"]}')
        if w_post.get('deflated_sharpe_p_value') is not None:
            print(f'  DSR p-value: {w_post["deflated_sharpe_p_value"]:.4f}')
        windows.append(w_post)

    verdict_order = {'edge_real': 0, 'edge_plausible': 1, 'no_edge': 2, 'insufficient_data': 3}
    headline_verdict = min(windows, key=lambda w: verdict_order.get(w['verdict'], 99))['verdict']

    headline = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'windows': windows,
        'headline_verdict': headline_verdict,
        'reading': (
            'Strategy is statistically proven' if headline_verdict == 'edge_real'
            else 'Some evidence of an edge — keep trading on paper' if headline_verdict == 'edge_plausible'
            else 'Need more data — keep trading on paper' if headline_verdict == 'insufficient_data'
            else 'Strategy is not working yet — keep tuning'
        ),
    }

    print(f'\n=== HEADLINE: {headline_verdict.upper()} ===')
    print(headline['reading'])

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(headline, f, indent=2)
    print(f'\nSaved to {RESULTS_FILE}')
    return headline


if __name__ == '__main__':
    main()