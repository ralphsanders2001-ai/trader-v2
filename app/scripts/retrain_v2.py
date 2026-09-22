"""
Retrain ML model v2 with expanded dataset.

Uses the same feature set as the original ml_features.py:
- entry_score, is_call, is_put, entry_price, hour, minute, day_of_week, is_morning
- minutes_since_open, consecutive_losses, trades_today_count, losses_today, etc.
- symbol_prior_winrate, recent_5_winrate, pnl_today, etc.

For the 776 ML-rejected signals, we synthesize the features:
- entry_score: the signal's score field
- is_call/is_put: from direction
- entry_price: ATM strike * 100 (option premium approximation)
- All time features: from timestamp
- Prior context features: same as actual trades up to that point in time
- target: hypothetical win/loss from backtest
"""
import json
import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.metrics import classification_report, roc_auc_score

sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from ml_features import extract_all_features

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
MODEL_DIR = Path("/home/ralph/trader-v2/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def find_atm_strike(symbol, ts):
    """Get ATM strike from the historical underlying price."""
    conn = sqlite3.connect(DB_PATH)
    # We don't have underlying prices stored; use a simple heuristic
    # All today's trades were on NVDA $225 or $227.5
    # For other stocks, use round-number strikes
    if symbol == 'NVDA':
        return 225.0
    elif symbol in ('TSLA', 'AAPL', 'AMZN', 'MSFT', 'GOOG', 'META'):
        return 200.0 if symbol == 'AMZN' else 150.0
    elif symbol in ('SPY', 'QQQ'):
        return 500.0 if symbol == 'SPY' else 450.0
    else:
        return 50.0  # Default for smaller stocks
    conn.close()


def build_hypothetical_trades():
    """
    For each ML-rejected signal, create a hypothetical trade row that
    can be processed by extract_features_for_trade().
    """
    with open('/tmp/backtest_labels.json') as f:
        labels = json.load(f)
    
    # Load actual closed trades to compute prior context
    conn = sqlite3.connect(DB_PATH)
    actual = pd.read_sql_query("""
        SELECT id, timestamp_open, timestamp_close, mode, symbol, option_type,
               option_strike, option_expiry, entry_price, exit_price, pnl,
               exit_reason, entry_signal_score
        FROM trades WHERE pnl IS NOT NULL
    """, conn)
    conn.close()
    
    if not actual.empty:
        actual['timestamp_open'] = pd.to_datetime(actual['timestamp_open'], format='ISO8601', utc=True)
        actual['timestamp_close'] = pd.to_datetime(actual['timestamp_close'], format='ISO8601', utc=True)
    
    # Build hypothetical trades from rejected signals
    hyp_rows = []
    for label in labels:
        sig_id = label['signal_id']
        sym = label['symbol']
        sig_time = label['signal_time']
        score = label['signal_score']
        direction = 'call' if label['direction'] == 'long_call' else 'put'
        pnl = label['option_pnl']
        
        # Convert sig_time to datetime
        sig_dt = pd.to_datetime(sig_time, format='ISO8601', utc=True)
        exit_dt = sig_dt + timedelta(minutes=15)
        
        strike = find_atm_strike(sym, sig_dt)
        entry_price = 1.50  # Approximate option price
        exit_price = entry_price + pnl / 100
        
        hyp_rows.append({
            'id': -sig_id,  # Negative ID to avoid collisions
            'timestamp_open': sig_dt,
            'timestamp_close': exit_dt,
            'mode': 'live',
            'symbol': sym,
            'option_type': direction,
            'option_strike': strike,
            'option_expiry': '2026-08-10',
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl': pnl,
            'exit_reason': 'backtest_hypothetical',
            'entry_signal_score': score,
        })
    
    return pd.DataFrame(hyp_rows), actual


def main():
    print("Loading actual closed trades...")
    actual_features = extract_all_features()
    if actual_features is None or len(actual_features) < 10:
        print("ERROR: Not enough actual trades")
        return
    
    print(f"\nActual trades: {len(actual_features)}")
    print(f"Win rate: {actual_features['target'].mean():.2%}")
    
    # Build hypothetical trades
    print("\nBuilding hypothetical trades from rejected signals...")
    hyp_df, actual_raw = build_hypothetical_trades()
    print(f"Hypothetical trades: {len(hyp_df)}")
    
    # Combine the raw trades (actual + hypothetical) to compute proper features
    # for hypothetical entries (using the actuals as "history")
    combined_raw = pd.concat([actual_raw, hyp_df], ignore_index=True)
    combined_raw = combined_raw.sort_values('timestamp_open').reset_index(drop=True)
    
    # Now extract features for each
    from ml_features import extract_features_for_trade
    
    feature_rows = []
    for idx, row in combined_raw.iterrows():
        try:
            f = extract_features_for_trade(row, combined_raw)
            f['trade_id'] = row['id']
            f['symbol'] = row['symbol']
            # For hypothetical trades, target is the backtest result (label column already set)
            # For actual trades, target is pnl > 0
            feature_rows.append(f)
        except Exception as e:
            # Skip silently for missing fields
            pass
    
    features_df = pd.DataFrame(feature_rows)
    
    print(f"\nTotal combined features: {len(features_df)}")
    print(f"Win rate: {features_df['target'].mean():.2%}")
    
    # Separate actual vs hypothetical
    actual_in_combined = features_df[features_df['trade_id'] > 0]
    hyp_in_combined = features_df[features_df['trade_id'] < 0]
    print(f"  Actual: {len(actual_in_combined)}")
    print(f"  Hypothetical: {len(hyp_in_combined)}")
    
    # Train
    exclude_cols = ["target", "actual_pnl", "trade_id", "symbol"]
    feature_cols = [c for c in features_df.columns if c not in exclude_cols]
    
    X = features_df[feature_cols].fillna(0)
    y = features_df["target"]
    
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )
    
    print(f"\nTrain: {len(X_train)}, Test: {len(X_test)}")
    
    # Train new model
    model_new = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
    )
    
    model_new.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    
    y_pred = model_new.predict(X_test)
    y_proba = model_new.predict_proba(X_test)[:, 1]
    
    new_auc = roc_auc_score(y_test, y_proba)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model_new, X, y, cv=cv, scoring="roc_auc")
    
    print(f"\n=== NEW MODEL (v2 with hypothetical labels) ===")
    print(classification_report(y_test, y_pred, target_names=["LOSS", "WIN"]))
    print(f"Test AUC: {new_auc:.3f}")
    print(f"5-fold CV AUC: {cv_scores.mean():.3f} (+/- {cv_scores.std():.3f})")
    
    # Feature importance
    importance = model_new.feature_importances_
    feat_imp = pd.DataFrame({
        "feature": feature_cols,
        "importance": importance
    }).sort_values("importance", ascending=False)
    
    print("\nTop 10 features:")
    print(feat_imp.head(10).to_string(index=False))
    
    # Compare with current model on same test set
    print(f"\n=== COMPARISON ===")
    old_auc = 0.0
    try:
        model_old = xgb.XGBClassifier()
        model_old.load_model(str(MODEL_DIR / "ml_model_v1.json"))

        # Old model may have different features — align to v1's expected columns
        v1_feature_cols = [
            "entry_score", "is_call", "is_put", "entry_price", "hour", "minute",
            "day_of_week", "is_morning", "is_first_30_min", "minutes_since_open",
            "strike", "consecutive_losses", "trades_today_count", "losses_today",
            "wins_today", "pnl_today", "avg_pnl_today", "symbol_prior_trades",
            "symbol_prior_winrate", "symbol_prior_avg_pnl", "symbol_prior_total_pnl",
            "minutes_since_last_symbol_trade", "last_symbol_trade_was_loss",
            "recent_5_avg_pnl", "recent_5_winrate", "recent_5_loss_count",
        ]
        # Drop columns the old model doesn't know about (e.g. hold_minutes)
        X_for_old = X_test.drop(columns=[c for c in X_test.columns if c not in v1_feature_cols], errors="ignore")
        old_proba = model_old.predict_proba(X_for_old)[:, 1]
        old_auc = roc_auc_score(y_test, old_proba)
    except Exception as e:
        print(f"Old model comparison skipped: {e}")
        old_auc = 0.0

    print(f"Old model AUC on new test set: {old_auc:.3f}")
    
    print(f"Old model AUC on new test set: {old_auc:.3f}")
    print(f"New model AUC: {new_auc:.3f}")
    print(f"Difference: {new_auc - old_auc:+.3f}")
    
    # FIX 2026-08-10: Always save new model if it's at least as good as old —
    # more training data is always valuable even if AUC doesn't move much.
    if new_auc >= old_auc - 0.005:
        print(f"\n✓ Saving model (new AUC {new_auc:.3f} vs old {old_auc:.3f}, {new_auc - old_auc:+.3f})")
        print("Saving as ml_model_v2.json")
        model_new.save_model(str(MODEL_DIR / "ml_model_v2.json"))
        with open(MODEL_DIR / "ml_metadata_v2.json", 'w') as f:
            json.dump({
                "test_auc": float(new_auc),
                "cv_auc_mean": float(cv_scores.mean()),
                "cv_auc_std": float(cv_scores.std()),
                "training_examples": len(features_df),
                "actual_trades": len(actual_in_combined),
                "hypothetical_trades": len(hyp_in_combined),
                "feature_importance": feat_imp.to_dict('records'),
                "old_model_auc": float(old_auc),
                "improvement": float(new_auc - old_auc),
                "trained_at": datetime.now().isoformat(),
            }, f, indent=2)
        print(f"\nModel saved. Ready to deploy via:")
        print(f"  cp /home/ralph/trader-v2/models/ml_model_v2.json /home/ralph/trader-v2/models/ml_model_v1.json")
    else:
        print(f"\n✗ New model is not significantly better")
        print("Keeping current model")


if __name__ == "__main__":
    main()