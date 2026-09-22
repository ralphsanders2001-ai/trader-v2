"""
Weekly ML Model Retraining Job

Runs Sunday night at 22:00 (after market close Friday 16:05 + weekend).

Pipeline:
1. Pull all scan decisions + outcomes from the DB
2. Add hypothetical labels for ML-rejected candidates (if price data available)
3. Split: train on data before last 7 days, test on last 7 days
4. Train new XGBoost model
5. Compare AUC vs current model
6. If new AUC > current + 0.02: deploy, backup old, save report
7. If new AUC <= current + 0.02: keep current, save report
8. Save everything to NAS: /mnt/file-cabinet/trader-v2/weekly-retraining/

Keeps last 100 backups on NAS (auto-cleanup).
"""
import os
import sys
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.metrics import roc_auc_score, classification_report

sys.path.insert(0, "/home/ralph/trader-v2/scripts")

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"
LOCAL_MODEL_DIR = Path("/home/ralph/trader-v2/models")
NAS_ROOT = Path("/mnt/file-cabinet/trader-v2/weekly-retraining")
NAS_MODELS = NAS_ROOT / "models"
NAS_DATA = NAS_ROOT / "training-data"
NAS_REPORTS = NAS_ROOT / "reports"
LOCAL_TRADE_DATA = Path("/home/ralph/trader-v2/trade-data")
BACKUP_DRIVE_ROOT = Path("/mnt/backup-mount/trader-v2")
BACKUP_MODELS = BACKUP_DRIVE_ROOT / "ml-training"
BACKUP_TRADE_DATA = BACKUP_DRIVE_ROOT / "trade-data"
BACKUP_HYPOTHETICAL = BACKUP_DRIVE_ROOT / "hypothetical-labels"
BACKUP_REPORTS = BACKUP_DRIVE_ROOT / "reports"
NAS_TRADE_DATA = Path("/mnt/file-cabinet/trader-v2/trade-data")

# Keep last 100 backups on NAS
MAX_BACKUPS_ON_NAS = 100


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dirs():
    for d in [NAS_MODELS, NAS_DATA, NAS_REPORTS, LOCAL_TRADE_DATA,
              NAS_TRADE_DATA, NAS_TRADE_DATA / "daily",
              BACKUP_DRIVE_ROOT, BACKUP_MODELS, BACKUP_TRADE_DATA,
              BACKUP_HYPOTHETICAL, BACKUP_REPORTS]:
        d.mkdir(parents=True, exist_ok=True)


def export_trade_data():
    """Export today's trade data and signals to NAS."""
    today = datetime.now().strftime("%Y-%m-%d")
    ts = timestamp()

    conn = sqlite3.connect(DB_PATH)

    # Export trades
    trades_df = pd.read_sql_query("SELECT * FROM trades", conn)
    signals_df = pd.read_sql_query("SELECT * FROM signals", conn)

    conn.close()

    # Save locally and to NAS and backup drive
    trades_file = LOCAL_TRADE_DATA / f"trades-{today}.csv"
    trades_df.to_csv(trades_file, index=False)

    # Backup drive (4TB local - primary training data store)
    backup_trades_file = BACKUP_TRADE_DATA / f"trades-{today}.csv"
    trades_df.to_csv(backup_trades_file, index=False)

    nas_trades_file = NAS_TRADE_DATA / "daily" / f"trades-{today}.csv"
    trades_df.to_csv(nas_trades_file, index=False)

    signals_file = LOCAL_TRADE_DATA / f"signals-{today}.csv"
    signals_df.to_csv(signals_file, index=False)

    backup_signals_file = BACKUP_TRADE_DATA / f"signals-{today}.csv"
    signals_df.to_csv(backup_signals_file, index=False)

    nas_signals_file = NAS_TRADE_DATA / "daily" / f"signals-{today}.csv"
    signals_df.to_csv(nas_signals_file, index=False)

    return {
        "trades_count": len(trades_df),
        "signals_count": len(signals_df),
        "date": today,
        "trades_file": str(nas_trades_file),
        "signals_file": str(nas_signals_file),
    }


def build_training_dataset():
    """
    Build training dataset combining:
    - All closed trades with real P&L
    - ML-rejected signals with hypothetical labels (if available)
    """
    conn = sqlite3.connect(DB_PATH)

    actual = pd.read_sql_query("""
        SELECT id, timestamp_open, timestamp_close, mode, symbol, option_type,
               option_strike, option_expiry, entry_price, exit_price, pnl,
               exit_reason, entry_signal_score
        FROM trades WHERE pnl IS NOT NULL
    """, conn)
    conn.close()

    if actual.empty:
        return None, None

    actual['timestamp_open'] = pd.to_datetime(actual['timestamp_open'], format='ISO8601', utc=True)
    actual['timestamp_close'] = pd.to_datetime(actual['timestamp_close'], format='ISO8601', utc=True)

    # Try to load hypothetical labels from multiple possible locations
    hyp_df = None
    hyp_search_paths = [
        LOCAL_TRADE_DATA / "latest_hypothetical_labels.json",
        BACKUP_HYPOTHETICAL / "latest.json",
        Path("/tmp/hypothetical_labels.json"),
    ]
    # Also find the most recent dated file in hypothetical-labels dirs
    for search_dir in [BACKUP_HYPOTHETICAL, NAS_TRADE_DATA / "hypothetical-labels",
                       Path("/tmp")]:
        if search_dir.exists():
            dated = sorted(search_dir.glob("2026-*.json"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            if dated:
                hyp_search_paths.insert(0, dated[0])

    for hyp_file in hyp_search_paths:
        if hyp_file.exists():
            try:
                print(f"  Loading hypothetical labels from: {hyp_file}")
                from retrain_v2 import build_hypothetical_trades
                hyp_df, _ = build_hypothetical_trades()
                print(f"  Loaded {len(hyp_df)} hypothetical labels")
                break
            except Exception as e:
                print(f"  Could not load from {hyp_file}: {e}")

    if hyp_df is None:
        print("  No hypothetical labels found - training on actual trades only")

    return actual, hyp_df


def train_model(features_df, feature_cols):
    """Train new XGBoost model."""
    X = features_df[feature_cols].fillna(0)
    y = features_df["target"]

    # Time-based split: last 7 days = test
    if 'timestamp_open' in features_df.columns:
        cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=7)
        train_mask = features_df['timestamp_open'] < cutoff
        test_mask = features_df['timestamp_open'] >= cutoff

        X_train = X[train_mask]
        X_test = X[test_mask]
        y_train = y[train_mask]
        y_test = y[test_mask]

        if len(X_test) < 5:
            # Fall back to random split
            print(f"Only {len(X_test)} test examples in last 7 days, using random split")
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=0.2, random_state=42, stratify=y
            )
    else:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

    model = xgb.XGBClassifier(
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

    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    return model, X_train, X_test, y_train, y_test


def cleanup_old_backups():
    """Keep only last MAX_BACKUPS_ON_NAS model files on NAS."""
    models = sorted(NAS_MODELS.glob("ml_model_*.json"), key=lambda p: p.stat().st_mtime)
    if len(models) > MAX_BACKUPS_ON_NAS:
        for old_model in models[:-MAX_BACKUPS_ON_NAS]:
            old_model.unlink()
            # Also remove associated metadata
            meta = old_model.with_name(old_model.stem.replace("ml_model_", "ml_metadata_") + ".json")
            if meta.exists():
                meta.unlink()


def main():
    print("=" * 60)
    print(f"Weekly ML Retraining — {datetime.now().isoformat()}")
    print("=" * 60)

    ensure_dirs()

    # Step 1: Export trade data
    print("\n[1/5] Exporting trade data to NAS...")
    trade_export = export_trade_data()
    print(f"  Trades: {trade_export['trades_count']}, Signals: {trade_export['signals_count']}")

    # Step 2: Build dataset
    print("\n[2/5] Building training dataset...")
    actual_df, hyp_df = build_training_dataset()

    if actual_df is None or len(actual_df) < 20:
        print(f"ERROR: Not enough actual trades ({len(actual_df) if actual_df is not None else 0})")
        return

    # Combine
    if hyp_df is not None and len(hyp_df) > 0:
        combined_raw = pd.concat([actual_df, hyp_df], ignore_index=True)
    else:
        combined_raw = actual_df

    combined_raw = combined_raw.sort_values('timestamp_open').reset_index(drop=True)

    # Extract features
    print("\n[3/5] Extracting features...")
    from ml_features import extract_features_for_trade

    feature_rows = []
    for idx, row in combined_raw.iterrows():
        try:
            f = extract_features_for_trade(row, combined_raw)
            f['trade_id'] = row['id']
            f['symbol'] = row['symbol']
            f['timestamp_open'] = row['timestamp_open']
            feature_rows.append(f)
        except Exception:
            pass

    features_df = pd.DataFrame(feature_rows)
    print(f"  Features: {len(features_df)}, Win rate: {features_df['target'].mean():.2%}")

    # Get feature columns from current model
    meta_path = LOCAL_MODEL_DIR / "ml_metadata_v1.json"
    if not meta_path.exists():
        print("ERROR: No current model metadata")
        return

    with open(meta_path) as f:
        old_meta = json.load(f)

    feature_cols = old_meta['feature_columns']

    # Step 4: Train new model
    print("\n[4/5] Training new model...")
    new_model, X_train, X_test, y_train, y_test = train_model(features_df, feature_cols)

    new_proba = new_model.predict_proba(X_test)[:, 1]
    new_auc = roc_auc_score(y_test, new_proba)

    # Load current model and compare
    current_model = xgb.XGBClassifier()
    current_model.load_model(str(LOCAL_MODEL_DIR / "ml_model_v1.json"))
    current_proba = current_model.predict_proba(X_test)[:, 1]
    current_auc = roc_auc_score(y_test, current_proba)

    improvement = new_auc - current_auc
    print(f"\n  Current AUC: {current_auc:.4f}")
    print(f"  New AUC:     {new_auc:.4f}")
    print(f"  Improvement: {improvement:+.4f}")

    # Step 5: Save report
    print("\n[5/5] Saving report and (possibly) deploying...")
    ts = timestamp()
    report = {
        "trained_at": datetime.now().isoformat(),
        "training_examples": len(features_df),
        "actual_trades": len(actual_df),
        "hypothetical_trades": len(hyp_df) if hyp_df is not None else 0,
        "train_size": len(X_train),
        "test_size": len(X_test),
        "train_win_rate": float(y_train.mean()),
        "test_win_rate": float(y_test.mean()),
        "current_model_auc": float(current_auc),
        "new_model_auc": float(new_auc),
        "improvement": float(improvement),
        "deployed": improvement > 0.02,
        "feature_importance": {
            feature_cols[i]: float(new_model.feature_importances_[i])
            for i in range(len(feature_cols))
        },
    }

    report_file = NAS_REPORTS / f"weekly-retrain-{ts}.json"
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"  Report: {report_file}")

    # Also save report to backup drive
    backup_report_file = BACKUP_REPORTS / f"weekly-retrain-{ts}.json"
    with open(backup_report_file, 'w') as f:
        json.dump(report, f, indent=2)

    # Save new model to NAS and backup drive
    new_model_path = NAS_MODELS / f"ml_model_{ts}.json"
    new_model.save_model(str(new_model_path))
    new_meta_path = new_model_path.with_name(f"ml_metadata_{ts}.json")
    with open(new_meta_path, 'w') as f:
        json.dump({**report, "model_path": str(new_model_path)}, f, indent=2)

    # Backup drive (4TB local - primary training data store)
    backup_model_path = BACKUP_MODELS / f"ml_model_{ts}.json"
    new_model.save_model(str(backup_model_path))
    backup_meta_path = BACKUP_MODELS / f"ml_metadata_{ts}.json"
    with open(backup_meta_path, 'w') as f:
        json.dump({**report, "model_path": str(backup_model_path)}, f, indent=2)

    # Deploy if better
    if improvement > 0.02:
        # Backup current
        backup_path = LOCAL_MODEL_DIR / f"ml_model_v1_pre_v2_{ts}.json"
        shutil.copy(LOCAL_MODEL_DIR / "ml_model_v1.json", backup_path)

        # Deploy
        shutil.copy(str(new_model_path), LOCAL_MODEL_DIR / "ml_model_v1.json")

        # Update metadata
        with open(LOCAL_MODEL_DIR / "ml_metadata_v1.json", 'w') as f:
            json.dump({
                **old_meta,
                "test_auc": float(new_auc),
                "improvement": float(improvement),
                "trained_at": datetime.now().isoformat(),
                "deployed_at": datetime.now().isoformat(),
                "previous_model_backup": str(backup_path),
            }, f, indent=2)

        print(f"  ✓ DEPLOYED new model to live (improvement +{improvement:.3f})")
        print(f"  Old model backed up to: {backup_path}")
    else:
        print(f"  ✗ NOT deployed (improvement only {improvement:+.3f})")

    # Cleanup old backups
    cleanup_old_backups()

    # Restart trader so it picks up the new model if deployed
    if improvement > 0.02:
        subprocess.run(['systemctl', '--user', 'restart', 'trader-v2.service'])
        print("  Trader daemon restarted with new model")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()