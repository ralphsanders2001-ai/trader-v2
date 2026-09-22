"""Per-trade XGBoost feature-contribution logger.

For every closed trade, this module:
1. Loads the current ML model
2. Reconstructs the feature vector that the model saw at entry time
3. Computes pred_contribs=True to get per-feature contributions
4. Stores the top-3 contributing features + full dict in trade_outcomes table

This is the single highest-value diagnostic per the research:
"the top-3 contributing features for that trade's WIN/LOSS — this gives
you a direct signal for which scoring component is currently broken / working."
"""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

PROJECT = Path('/home/ralph/trader-v2')
MODEL_PATH = PROJECT / 'models' / 'ml_model_v1.json'
ARCHIVE_DIR = PROJECT / 'data' / 'model_archive'
PAPER_DB = PROJECT / 'data' / 'trader_paper.db'

# Must match the 16-feature set used in retrain_v2_paper.py
FEATURE_COLS = [
    'rsi_value', 'macd_hist', 'ema_9', 'ema_20', 'vwap',
    'volume_ratio', 'bb_position', 'vix', 'spread_pct',
    'hour_of_day', 'prior_day_tod_trend', 'entry_signal_score',
    'regime_id', 'vol_zscore', 'trend_slope_pct', 'realized_vol_pct',
]


def _load_model():
    """Load current model. Returns (model, metadata) or (None, None)."""
    try:
        import xgboost as xgb
        if not MODEL_PATH.exists():
            return None, None
        model = xgb.XGBClassifier()
        model.load_model(MODEL_PATH)
        meta_path = MODEL_PATH.with_suffix('.meta.json')
        meta = {}
        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
        return model, meta
    except Exception as e:
        return None, {'error': str(e)}


def _get_entry_features(trade_id: int) -> Optional[dict]:
    """Reconstruct the entry-time feature vector for a trade."""
    conn = sqlite3.connect(PAPER_DB)
    try:
        row = conn.execute("""
            SELECT t.entry_signal_score,
                   s.rsi_value, s.macd_hist, s.ema_9, s.ema_20, s.vwap,
                   s.volume_ratio, s.bb_position, s.vix, s.spread_pct,
                   s.hour_of_day, s.prior_day_tod_trend, s.ml_score,
                   s.regime_id, s.vol_zscore, s.trend_slope_pct, s.realized_vol_pct,
                   s.regime_label
            FROM trades t
            LEFT JOIN signals s ON s.trade_id = t.id
            WHERE t.id = ?
        """, (trade_id,)).fetchone()
        if row is None:
            return None
        cols = ['entry_signal_score',
                'rsi_value', 'macd_hist', 'ema_9', 'ema_20', 'vwap',
                'volume_ratio', 'bb_position', 'vix', 'spread_pct',
                'hour_of_day', 'prior_day_tod_trend', 'ml_score',
                'regime_id', 'vol_zscore', 'trend_slope_pct', 'realized_vol_pct',
                'regime_label']
        d = dict(zip(cols, row))
        # ml_score from signals is NOT the same as entry_signal_score for this model.
        # prior_day_tod_trend IS in the feature list — fill 0 if missing.
        return d
    finally:
        conn.close()


def _compute_contributions(model, features: dict) -> Optional[tuple]:
    """Compute the model's probability + per-feature influence on this trade.

    For XGBoost, two complementary signals:

    (a) pred_contribs=True — SHAP-style marginal contribution per feature.
        Only works on models trained with that objective setting. If it
        returns non-zero we use it. Otherwise we fall back to (b).

    (b) feature_importances_ — global gain per feature. We multiply by the
        feature's current value to get a per-trade signed proxy.

    Returns (proba, full_contribs_dict, top3_list, missing_cols, method) or None.
    """
    try:
        import pandas as pd
        import xgboost as xgb

        # Build the 16-feature vector in the exact order the model expects
        row = []
        missing = []
        for col in FEATURE_COLS:
            v = features.get(col)
            if v is None:
                v = 0.0
                missing.append(col)
            row.append(float(v))
        X = pd.DataFrame([row], columns=FEATURE_COLS)

        # Always compute probability
        proba = float(model.predict_proba(X)[0, 1])

        per_feat = None
        method = None

        # Try pred_contribs (true SHAP marginal)
        try:
            contribs = model.get_booster().predict(
                xgb.DMatrix(X), pred_contribs=True
            )[0]
            if len(contribs) == len(FEATURE_COLS) + 1:
                vals = contribs[:len(FEATURE_COLS)].tolist()
                if any(abs(v) > 1e-9 for v in vals):
                    per_feat = dict(zip(FEATURE_COLS, vals))
                    method = 'pred_contribs'
        except Exception:
            pass

        # Fallback: gain importance × feature value
        if per_feat is None:
            try:
                importance = model.feature_importances_
                per_feat = {}
                for col, imp in zip(FEATURE_COLS, importance):
                    val = X[col].iloc[0]
                    per_feat[col] = float(imp * val)
                method = 'gain_x_value'
            except Exception:
                per_feat = {col: 0.0 for col in FEATURE_COLS}
                method = 'unavailable'

        # Top-3 by absolute contribution
        ranked = sorted(per_feat.items(), key=lambda kv: abs(kv[1]), reverse=True)
        top3 = [{'feature': k, 'contribution': round(v, 6)} for k, v in ranked[:3]]
        full = {k: round(v, 6) for k, v in per_feat.items()}
        return proba, full, top3, missing, method
    except Exception:
        return None


def log_trade_outcome(trade_id: int) -> Optional[int]:
    """Compute and persist feature-contribution diagnostics for one trade.

    Idempotent — if a row already exists for this trade_id, it is replaced.
    Returns the row id, or None if no model is available.
    """
    import xgboost as xgb

    model, meta = _load_model()
    if model is None:
        return None

    feats = _get_entry_features(trade_id)
    if feats is None:
        return None

    result = _compute_contributions(model, feats)
    if result is None:
        return None
    proba, full, top3, missing, method = result

    # Trade + outcome info
    conn = sqlite3.connect(PAPER_DB)
    try:
        row = conn.execute("""
            SELECT t.timestamp_close, t.symbol, t.pnl, t.exit_reason,
                   t.entry_signal_score
            FROM trades t WHERE t.id = ?
        """, (trade_id,)).fetchone()
        if row is None or row[0] is None or row[2] is None:
            return None
        ts_close, symbol, pnl, exit_reason, entry_score = row

        outcome = 'win' if pnl > 0 else 'loss'
        recommendation = 'BUY' if proba >= 0.50 else 'SKIP'

        conn.execute("""
            INSERT OR REPLACE INTO trade_outcomes (
                trade_id, timestamp, symbol, pnl, outcome,
                ml_probability, ml_recommendation,
                top3_features_json, feature_contribs_json,
                model_auc, regime_id, regime_label, exit_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            ts_close,
            symbol,
            pnl,
            outcome,
            proba,
            recommendation,
            json.dumps(top3),
            json.dumps(full),
            meta.get('test_auc'),
            feats.get('regime_id'),
            feats.get('regime_label'),
            exit_reason,
        ))
        conn.commit()
        return trade_id
    finally:
        conn.close()


def backfill_all() -> dict:
    """Compute contributions for all closed paper trades that don't have one yet.

    Called once on startup so we have data for every existing trade.
    """
    conn = sqlite3.connect(PAPER_DB)
    try:
        rows = conn.execute("""
            SELECT t.id FROM trades t
            WHERE t.timestamp_close IS NOT NULL
              AND t.pnl IS NOT NULL
              AND t.id NOT IN (SELECT trade_id FROM trade_outcomes WHERE trade_id IS NOT NULL)
            ORDER BY t.id
        """).fetchall()
    finally:
        conn.close()

    done = 0
    failed = 0
    for (tid,) in rows:
        try:
            if log_trade_outcome(tid) is not None:
                done += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    return {'processed': len(rows), 'logged': done, 'failed': failed}


if __name__ == '__main__':
    print('=== Backfilling feature contributions for all closed trades ===')
    result = backfill_all()
    print(result)

    print('\n=== Sample of trade_outcomes ===')
    conn = sqlite3.connect(PAPER_DB)
    try:
        rows = conn.execute("""
            SELECT trade_id, symbol, outcome, pnl, ml_probability, top3_features_json
            FROM trade_outcomes ORDER BY trade_id DESC LIMIT 10
        """).fetchall()
        for tid, sym, outcome, pnl, prob, top3 in rows:
            print(f"  #{tid} {sym} {outcome} pnl={pnl:+.0f} P(profit)={prob:.2%} top3={top3}")
    finally:
        conn.close()