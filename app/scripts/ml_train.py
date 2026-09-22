"""
ML Model Trainer.

Trains an XGBoost classifier on extracted features to predict whether a new
trade will be profitable (target = pnl > 0).

The trained model is saved to /home/ralph/trader-v2/models/ml_model_v1.json
and is loaded by ml_predict.py at scan time.
"""
import os
import sys
import sqlite3
import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit, cross_val_score

# FIX 2026-08-13: Replaced random train_test_split with TimeSeriesSplit.
# Random split leaks future data into the training set (lookahead bias)
# which makes the model look much better on paper than it actually is.
# TimeSeriesSplit honors the temporal order — train on past, test on future.
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score

# Import feature extractor
sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from ml_features import extract_all_features


MODEL_DIR = Path("/home/ralph/trader-v2/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / "ml_model_v1.json"
METADATA_PATH = MODEL_DIR / "ml_metadata_v1.json"


def train_model(features_df):
    """Train XGBoost classifier and return the trained model."""
    # Define feature columns (exclude non-feature cols)
    exclude_cols = ["target", "actual_pnl", "trade_id", "symbol", "hold_minutes"]
    feature_cols = [c for c in features_df.columns if c not in exclude_cols]

    X = features_df[feature_cols].fillna(0)
    y = features_df["target"]

    print(f"\nTraining data shape: {X.shape}")
    print(f"Win rate in training set: {y.mean():.2%}")
    print(f"\nFeature columns ({len(feature_cols)}):")
    for col in feature_cols:
        print(f"  {col}")

    # FIX 2026-08-13: Time-series split — train on past, test on future.
    # Sort by id (which is autoincrement = chronological) to align with time.
    X = X.reset_index(drop=True)
    y = y.reset_index(drop=True)
    split = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split], X.iloc[split:]
    y_train, y_test = y.iloc[:split], y.iloc[split:]

    if len(X_test) < 5 or y_test.nunique() < 2:
        print(f"\n⚠ Insufficient test data for evaluation (size={len(X_test)}, classes={y_test.nunique()})")
        print(f"Train size: {len(X_train)}, Test size: {len(X_test)}")
    else:
        print(f"\nTrain size: {len(X_train)}, Test size: {len(X_test)}")
        print(f"Train win rate: {y_train.mean():.2%}")
        print(f"Test win rate: {y_test.mean():.2%}")

    # XGBoost classifier with conservative params (small dataset)
    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=3,           # shallow trees to prevent overfitting
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,    # require at least 3 samples per leaf
        reg_alpha=0.1,         # L1 regularization
        reg_lambda=1.0,        # L2 regularization
        scale_pos_weight=(1-y_train.mean())/y_train.mean(),  # handle class imbalance
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        use_label_encoder=False,
    )

    # Train
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )

    # Evaluate
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    print("\n" + "=" * 50)
    print("Test Set Performance")
    print("=" * 50)
    print(classification_report(y_test, y_pred, target_names=["LOSS", "WIN"]))
    print(f"Test ROC AUC: {roc_auc_score(y_test, y_proba):.3f}")

    # Cross-validation on full dataset — TimeSeriesSplit (no shuffle, no leakage)
    # FIX 2026-08-13: replaced shuffled StratifiedKFold with TimeSeriesSplit
    cv = TimeSeriesSplit(n_splits=5) if len(X) >= 60 else TimeSeriesSplit(n_splits=max(2, len(X) // 12))
    cv_scores = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")
    print(f"\n5-fold CV ROC AUC: {cv_scores.mean():.3f} (+/- {cv_scores.std():.3f})")

    # Feature importance
    importance = model.feature_importances_
    feat_imp = pd.DataFrame({
        "feature": feature_cols,
        "importance": importance
    }).sort_values("importance", ascending=False)

    print("\nTop 10 Features:")
    print(feat_imp.head(10).to_string(index=False))

    return model, feature_cols, {
        "test_auc": float(roc_auc_score(y_test, y_proba)),
        "cv_auc_mean": float(cv_scores.mean()),
        "cv_auc_std": float(cv_scores.std()),
        "train_size": len(X_train),
        "test_size": len(X_test),
        "train_win_rate": float(y_train.mean()),
        "test_win_rate": float(y_test.mean()),
        "feature_importance": feat_imp.to_dict(orient="records"),
    }


def save_model(model, feature_cols, metadata):
    """Save model + metadata."""
    model.save_model(str(MODEL_PATH))
    metadata["feature_columns"] = feature_cols
    metadata["trained_at"] = datetime.now().isoformat()
    metadata["model_path"] = str(MODEL_PATH)
    metadata["model_type"] = "XGBClassifier"

    with open(METADATA_PATH, "w") as f:
        json.dump(metadata, f, indent=2, default=str)

    print(f"\nModel saved to: {MODEL_PATH}")
    print(f"Metadata saved to: {METADATA_PATH}")


if __name__ == "__main__":
    print("=" * 60)
    print("ML Model Trainer")
    print("=" * 60)

    features_df = extract_all_features()
    if features_df is None or len(features_df) < 20:
        print("ERROR: Not enough data to train (need 20+ trades)")
        sys.exit(1)

    model, feature_cols, metadata = train_model(features_df)
    save_model(model, feature_cols, metadata)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
