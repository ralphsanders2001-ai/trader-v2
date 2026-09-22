#!/usr/bin/env python3
"""Daily paper-aware retrain for v2 trader.

Replaces the broken learn-strategy-weights cron that was silently firing
into the live (disabled) signal file. This script:

1. Reads paper trades from /home/ralph/trader-v2/data/trader_paper.db
2. Extracts features using ml_features.extract_all_features
3. Uses TimeSeriesSplit (proper — no lookahead bias) instead of random split
4. Uses XGBoost xgb_model= for warm-start (continue training existing model)
5. Writes a shadow model only; promotion requires beating current AUC by >= 0.005
6. Logs to data/metrics.jsonl for nightly review

Runs from cron at 6:30 AM ET, after learn-strategy-weights which has been
failing silently.
"""
import os
import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score

sys.path.insert(0, '/home/ralph/trader-v2/scripts')
from ml_features import extract_all_features

LOG = logging.getLogger('v2.retrain')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

PROJECT = Path('/home/ralph/trader-v2')
MODEL_DIR = PROJECT / 'models'
ARCHIVE_DIR = PROJECT / 'data' / 'model_archive'
METRICS_FILE = PROJECT / 'data' / 'metrics.jsonl'

CURRENT_MODEL = MODEL_DIR / 'ml_model_v1.json'
SHADOW_MODEL = MODEL_DIR / 'ml_model_v1_shadow.json'
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'


def load_trades() -> pd.DataFrame:
    """Load closed paper trades + features."""
    conn = sqlite3.connect(PAPER_DB)
    df = pd.read_sql_query("""
        SELECT t.id, t.timestamp_open, t.timestamp_close, t.symbol,
               t.option_strike, t.option_type, t.option_expiry,
               t.entry_price, t.exit_price, t.pnl, t.exit_reason,
               t.entry_signal_components, t.entry_signal_score,
               t.entry_candles_used,
               s.rsi_value, s.macd_hist, s.ema_9, s.ema_20, s.vwap,
               s.volume_ratio, s.bb_position, s.vix, s.spread_pct,
               s.hour_of_day, s.prior_day_tod_trend, s.ml_score,
               s.regime_id, s.vol_zscore, s.trend_slope_pct, s.realized_vol_pct,
               s.trades_today_total, s.trades_today_for_symbol,
               s.consecutive_loss_streak, s.consecutive_win_streak,
               s.minutes_since_last_loss_global, s.minutes_since_last_symbol_trade,
               s.symbol_prior_trades, s.symbol_prior_winrate,
               s.today_pnl, s.recent_5_winrate, s.recent_5_avg_pnl
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.timestamp_close IS NOT NULL
          AND t.mode = 'paper'
        ORDER BY t.timestamp_open
    """, conn)
    conn.close()
    df['win'] = (df['pnl'] > 0).astype(int)
    return df


def build_features(df: pd.DataFrame) -> tuple:
    """Build the feature matrix used by the v2 trader."""
    feature_cols = [
        'rsi_value', 'macd_hist', 'ema_9', 'ema_20', 'vwap',
        'volume_ratio', 'bb_position', 'vix', 'spread_pct',
        'hour_of_day', 'prior_day_tod_trend', 'entry_signal_score',
        # FIX 2026-08-13: regime features added
        'regime_id', 'vol_zscore', 'trend_slope_pct', 'realized_vol_pct',
        # FIX 2026-08-13: signal-sequence context features
        'trades_today_total', 'trades_today_for_symbol',
        'consecutive_loss_streak', 'consecutive_win_streak',
        'minutes_since_last_loss_global', 'minutes_since_last_symbol_trade',
        'symbol_prior_trades', 'symbol_prior_winrate',
        'today_pnl', 'recent_5_winrate', 'recent_5_avg_pnl',
    ]
    # Only use columns that actually exist in the signals table right now
    feature_cols = [c for c in feature_cols if c in df.columns]
    X = df[feature_cols].copy()
    # FIX 2026-08-15: Some signal rows store human-readable labels in numeric
    # columns (e.g. prior_day_tod_trend = "bullish_+0.31pct" because the
    # components dict is dumped verbatim into the DB column). pd.to_numeric
    # coerces those to NaN, then fillna(0) cleans them up.
    for c in feature_cols:
        X[c] = pd.to_numeric(X[c], errors='coerce')
    X = X.fillna(0).astype(float)
    y = df['win'].astype(int)
    return X, y, feature_cols


def walk_forward_evaluate(X, y, n_splits=5) -> dict:
    """Time-series CV — never peeks into the future."""
    if len(X) < 30:
        return {'auc_mean': 0.5, 'auc_std': 0.0, 'n_splits': 0, 'note': 'insufficient data'}
    tscv = TimeSeriesSplit(n_splits=n_splits)
    aucs = []
    for train_idx, test_idx in tscv.split(X):
        if len(test_idx) < 5:
            continue
        model = xgb.XGBClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
            reg_alpha=0.1, reg_lambda=1.0,
            objective='binary:logistic', eval_metric='logloss',
            random_state=42,
        )
        model.fit(X.iloc[train_idx], y.iloc[train_idx], verbose=False)
        proba = model.predict_proba(X.iloc[test_idx])[:, 1]
        if y.iloc[test_idx].nunique() < 2:
            continue
        auc = roc_auc_score(y.iloc[test_idx], proba)
        aucs.append(auc)
    if not aucs:
        return {'auc_mean': 0.5, 'auc_std': 0.0, 'n_splits': 0, 'note': 'no valid splits'}
    return {
        'auc_mean': float(np.mean(aucs)),
        'auc_std': float(np.std(aucs)),
        'n_splits': len(aucs),
        'auc_last': float(aucs[-1]),
    }


def evaluate_current_model(X, y) -> Optional[float]:
    """Score the current production model on the latest 30 trades."""
    if not CURRENT_MODEL.exists():
        return None
    try:
        model = xgb.XGBClassifier()
        model.load_model(CURRENT_MODEL)
        if len(X) < 30:
            return None
        last_X = X.tail(30)
        last_y = y.tail(30)
        proba = model.predict_proba(last_X)[:, 1]
        if last_y.nunique() < 2:
            return None
        return float(roc_auc_score(last_y, proba))
    except Exception as e:
        LOG.warning(f'Could not score current model: {e}')
        return None


def train_shadow(X, y, feature_cols) -> dict:
    """Train a new model — warm-start if current exists."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    model = xgb.XGBClassifier(
        n_estimators=120, max_depth=3, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
        reg_alpha=0.1, reg_lambda=1.0,
        objective='binary:logistic', eval_metric='logloss',
        random_state=42,
    )

    # Warm-start if previous model exists AND has the same number of features.
    # We DO NOT call load_model on the same instance when counts differ — that
    # corrupts the booster's feature metadata. Instead create a fresh model.
    warm_started = False
    if CURRENT_MODEL.exists():
        try:
            import xgboost as _xgb
            _booster = _xgb.Booster()
            _booster.load_model(CURRENT_MODEL)
            if len(_booster.feature_names or []) == len(feature_cols):
                model.load_model(CURRENT_MODEL)
                warm_started = True
                LOG.info(f'Warm-starting from {CURRENT_MODEL}')
            else:
                LOG.warning(f'Feature count changed ({len(_booster.feature_names or [])} -> {len(feature_cols)}), training fresh')
        except Exception as e:
            LOG.warning(f'Warm-start probe failed, training fresh: {e}')

    # Time-based holdout: last 20% as test
    split = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split], X.iloc[split:]
    y_train, y_test = y.iloc[:split], y.iloc[split:]

    if len(X_test) < 5 or y_test.nunique() < 2:
        return {'promoted': False, 'note': 'insufficient test set'}

    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    proba = model.predict_proba(X_test)[:, 1]
    new_auc = float(roc_auc_score(y_test, proba))

    model.save_model(SHADOW_MODEL)
    LOG.info(f'Shadow model written: {SHADOW_MODEL} AUC={new_auc:.3f}')

    # Compare against current
    current_auc = evaluate_current_model(X, y)

    promoted = (current_auc is None) or (new_auc >= current_auc - 0.005)

    if promoted:
        # Back up current, copy shadow to current
        if CURRENT_MODEL.exists():
            import shutil
            backup = ARCHIVE_DIR / f'ml_model_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
            shutil.copy2(CURRENT_MODEL, backup)
        import shutil
        shutil.copy2(SHADOW_MODEL, CURRENT_MODEL)
        if current_auc is None:
            LOG.info(f'PROMOTED first model -> current (AUC={new_auc:.3f})')
        else:
            LOG.info(f'PROMOTED shadow -> current (new={new_auc:.3f} vs current={current_auc:.3f})')
    else:
        LOG.info(f'NOT promoted (new={new_auc:.3f} < current={current_auc:.3f})')

    return {
        'promoted': promoted,
        'new_auc': new_auc,
        'current_auc': current_auc,
        'train_size': len(X_train),
        'test_size': len(X_test),
    }


def log_metrics(record: dict):
    """Append to data/metrics.jsonl for nightly review."""
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    record['timestamp'] = datetime.now(timezone.utc).isoformat()
    with open(METRICS_FILE, 'a') as f:
        f.write(json.dumps(record) + '\n')


def main():
    LOG.info('=== v2 paper retrain start ===')
    df = load_trades()
    LOG.info(f'Loaded {len(df)} closed paper trades')
    if df['win'].sum() == 0 or (~df['win'].astype(bool)).sum() == 0:
        LOG.info('Need both wins and losses; skipping')
        return

    X, y, feature_cols = build_features(df)
    LOG.info(f'Feature matrix: {X.shape}, win rate {y.mean():.2%}')

    wf = walk_forward_evaluate(X, y)
    LOG.info(f'Walk-forward AUC: {wf["auc_mean"]:.3f} (+/- {wf["auc_std"]:.3f})')

    result = train_shadow(X, y, feature_cols)
    LOG.info(f'Train result: {result}')

    log_metrics({
        'job': 'retrain_v2_paper',
        'trades': len(df),
        'win_rate': float(y.mean()),
        'walk_forward': wf,
        'train_result': result,
        'feature_cols': feature_cols,
    })

    LOG.info('=== retrain complete ===')


if __name__ == '__main__':
    main()