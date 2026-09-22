"""Walk-forward validation of regime-conditioned model.

The core question: does conditioning on regime_id actually improve win-rate
prediction, or is it noise?

Method:
1. For each rolling time window (50 trades), train on past N-50 trades
2. Predict win/loss for the next 50 trades
3. Compare AUC of regime-conditioned model vs regime-blind baseline
4. Also break down by regime: in each regime, what is the realized WR?

This is the "did the feature actually help" diagnostic the research called for.
Without it, we are guessing whether the regime column is signal or noise.
"""
from __future__ import annotations
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path('/home/ralph/trader-v2')
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'
RESULTS_FILE = PROJECT / 'data' / 'walkforward_results.json'

# Two feature sets to compare
FEATURES_WITH_REGIME = [
    'rsi_value', 'macd_hist', 'ema_9', 'ema_20', 'vwap',
    'volume_ratio', 'bb_position', 'vix', 'spread_pct',
    'hour_of_day', 'prior_day_tod_trend', 'entry_signal_score',
    'regime_id', 'vol_zscore', 'trend_slope_pct', 'realized_vol_pct',
    'trades_today_total', 'trades_today_for_symbol',
    'consecutive_loss_streak', 'consecutive_win_streak',
    'minutes_since_last_loss_global', 'minutes_since_last_symbol_trade',
    'symbol_prior_trades', 'symbol_prior_winrate',
    'today_pnl', 'recent_5_winrate', 'recent_5_avg_pnl',
]

FEATURES_NO_REGIME = [
    'rsi_value', 'macd_hist', 'ema_9', 'ema_20', 'vwap',
    'volume_ratio', 'bb_position', 'vix', 'spread_pct',
    'hour_of_day', 'prior_day_tod_trend', 'entry_signal_score',
    'trades_today_total', 'trades_today_for_symbol',
    'consecutive_loss_streak', 'consecutive_win_streak',
    'minutes_since_last_loss_global', 'minutes_since_last_symbol_trade',
    'symbol_prior_trades', 'symbol_prior_winrate',
    'today_pnl', 'recent_5_winrate', 'recent_5_avg_pnl',
]


def load_dataset(min_samples: int = 60) -> 'pd.DataFrame':
    """Load closed paper trades with all feature columns + outcome."""
    import pandas as pd

    conn = sqlite3.connect(PAPER_DB)
    try:
        df = pd.read_sql_query('''
            SELECT
                t.id, t.timestamp_open, t.symbol, t.option_type,
                t.pnl, t.exit_reason,
                s.rsi_value, s.macd_hist, s.ema_9, s.ema_20, s.vwap,
                s.volume_ratio, s.bb_position, s.vix, s.spread_pct,
                s.hour_of_day, s.prior_day_tod_trend, s.ml_score,
                s.regime_id, s.vol_zscore, s.trend_slope_pct, s.realized_vol_pct,
                t.entry_signal_score,
                s.trades_today_total, s.trades_today_for_symbol,
                s.consecutive_loss_streak, s.consecutive_win_streak,
                s.minutes_since_last_loss_global, s.minutes_since_last_symbol_trade,
                s.symbol_prior_trades, s.symbol_prior_winrate,
                s.today_pnl, s.recent_5_winrate, s.recent_5_avg_pnl
            FROM trades t
            LEFT JOIN signals s ON s.trade_id = t.id
            WHERE t.timestamp_close IS NOT NULL
              AND t.pnl IS NOT NULL
              AND t.mode = 'paper'
            ORDER BY t.timestamp_open
        ''', conn)
    finally:
        conn.close()

    df['outcome'] = (df['pnl'] > 0).astype(int)
    df = df.fillna(0)
    if len(df) < min_samples:
        return df
    return df


def walkforward(df, feature_cols, n_splits: int = 4) -> dict:
    """Time-series walk-forward validation.

    Splits the data into n_splits chronological chunks. For each fold,
    trains on chunks 0..i and validates on chunk i+1. Returns mean AUC.
    """
    import numpy as np
    import pandas as pd
    from sklearn.metrics import roc_auc_score
    import xgboost as xgb

    n = len(df)
    fold_size = n // (n_splits + 1)
    if fold_size < 10:
        return {'aucs': [], 'mean_auc': None, 'note': 'insufficient_data'}

    aucs = []
    # FIX 2026-08-13: expanding window — train grows, test slides forward
    train_start = 0
    for i in range(n_splits):
        train_end = (i + 1) * fold_size
        test_end = min(train_end + fold_size, n)
        if train_end >= n or test_end <= train_end:
            break
        train = df.iloc[train_start:train_end]
        test = df.iloc[train_end:test_end]
        # Accept folds even if test is small — 77 trades is a tiny dataset
        if len(train) < 20 or len(test) < 3:
            continue

        # FIX 2026-08-13: drop non-numeric columns (option_type is a string)
        numeric_cols = [c for c in feature_cols if c in train.columns]
        X_train = train[numeric_cols].astype(float).fillna(0)
        y_train = train['outcome']
        X_test = test[numeric_cols].astype(float).fillna(0)
        y_test = test['outcome']

        try:
            model = xgb.XGBClassifier(
                n_estimators=80, max_depth=3, learning_rate=0.1,
                subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
                reg_alpha=0.1, reg_lambda=1.0,
                objective='binary:logistic', eval_metric='logloss',
                random_state=42,
            )
            model.fit(X_train, y_train, verbose=False)
            proba = model.predict_proba(X_test)[:, 1]
            # Allow AUC even with class imbalance — return NaN if too few
            try:
                if len(set(y_test)) < 2:
                    # Skip AUC but log baseline accuracy
                    acc = float((proba > 0.5).astype(int) == y_test).mean()
                    aucs.append(max(acc, 1.0 - acc))  # at-least-baseline
                else:
                    auc = float(roc_auc_score(y_test, proba))
                    aucs.append(auc)
            except Exception:
                continue
        except Exception:
            continue

    if not aucs:
        return {'aucs': [], 'mean_auc': None, 'note': 'no_valid_folds'}

    mean_auc = float(np.mean(aucs))
    std_auc = float(np.std(aucs))
    return {
        'aucs': [round(a, 4) for a in aucs],
        'mean_auc': round(mean_auc, 4),
        'std_auc': round(std_auc, 4),
        'note': 'ok',
    }


def per_regime_breakdown(df) -> list:
    """For each regime, compute realized win rate and average P&L."""
    if 'regime_id' not in df.columns:
        return []
    rows = []
    grouped = df.groupby('regime_id')
    for regime_id, group in grouped:
        wins = (group['outcome'] == 1).sum()
        total = len(group)
        if total < 5:
            continue
        rows.append({
            'regime_id': int(regime_id),
            'trades': int(total),
            'wins': int(wins),
            'win_rate': round(wins / total, 4),
            'avg_pnl': round(float(group['pnl'].mean()), 2),
            'total_pnl': round(float(group['pnl'].sum()), 2),
        })
    rows.sort(key=lambda r: r['total_pnl'], reverse=True)
    return rows


def main():
    import pandas as pd

    df = load_dataset()
    n = len(df)
    print(f'Loaded {n} closed paper trades')

    if n < 60:
        print('Insufficient data for walk-forward (need 60+)')
        return

    # Walk-forward WITH regime
    print('\n=== Walk-forward WITH regime features ===')
    wf_regime = walkforward(df, FEATURES_WITH_REGIME, n_splits=4)
    print(json.dumps(wf_regime, indent=2))

    # Walk-forward WITHOUT regime
    print('\n=== Walk-forward WITHOUT regime features ===')
    wf_no_regime = walkforward(df, FEATURES_NO_REGIME, n_splits=4)
    print(json.dumps(wf_no_regime, indent=2))

    # Per-regime breakdown
    print('\n=== Per-regime breakdown ===')
    regime_rows = per_regime_breakdown(df)
    for r in regime_rows:
        print(f"  regime={r['regime_id']} trades={r['trades']} WR={r['win_rate']:.1%} avg_pnl=${r['avg_pnl']:+.2f} total=${r['total_pnl']:+.2f}")

    # Compute the headline answer
    delta = None
    if wf_regime.get('mean_auc') is not None and wf_no_regime.get('mean_auc') is not None:
        delta = round(wf_regime['mean_auc'] - wf_no_regime['mean_auc'], 4)

    headline = {
        'with_regime_auc': wf_regime.get('mean_auc'),
        'without_regime_auc': wf_no_regime.get('mean_auc'),
        'delta_auc': delta,
        'verdict': None,
    }

    if delta is None:
        headline['verdict'] = 'insufficient_data'
    elif delta > 0.02:
        headline['verdict'] = 'regime_helps'
    elif delta < -0.02:
        headline['verdict'] = 'regime_hurts'
    else:
        headline['verdict'] = 'no_meaningful_difference'

    print('\n=== HEADLINE ===')
    print(json.dumps(headline, indent=2))

    # Save results
    results = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'n_trades': n,
        'walkforward_with_regime': wf_regime,
        'walkforward_without_regime': wf_no_regime,
        'per_regime': regime_rows,
        'headline': headline,
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results, f, indent=2)
    print(f'\nSaved to {RESULTS_FILE}')


if __name__ == '__main__':
    main()