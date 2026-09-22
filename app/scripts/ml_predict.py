"""
ML Predictor — used at scan time to decide if a candidate trade is worth taking.

Loads the trained XGBoost model and predicts P(profitable) for a candidate
trade given its features.

Usage:
    from ml_predict import predict_trade_profitability
    prob = predict_trade_profitability({
        "symbol": "NVDA",
        "option_type": "call",
        "option_strike": 220.0,
        "entry_price": 2.50,
        "entry_signal_score": 45,
        ...
    })
    # prob = 0.42 — only enter if prob >= 0.40
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import xgboost as xgb
import sqlite3

sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from ml_features import get_connection


MODEL_PATH = Path("/home/ralph/trader-v2/models/ml_model_v1.json")
METADATA_PATH = Path("/home/ralph/trader-v2/models/ml_metadata_v1.json")


# Lazy-loaded model cache
_model = None
_metadata = None


def _load_model():
    """Lazy-load model on first use."""
    global _model, _metadata
    if _model is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"Model not found: {MODEL_PATH}. Run ml_train.py first.")
        _model = xgb.XGBClassifier()
        _model.load_model(str(MODEL_PATH))
        with open(METADATA_PATH) as f:
            _metadata = json.load(f)
    return _model, _metadata


def _get_current_context(symbol, option_type, option_strike, entry_price, entry_score):
    """Build feature vector for a CANDIDATE (not-yet-entered) trade."""
    model, metadata = _load_model()
    feature_cols = metadata["feature_columns"]

    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT id, timestamp_open, timestamp_close, symbol, option_type,
               option_strike, entry_price, exit_price, pnl, exit_reason,
               entry_signal_score
        FROM trades
        WHERE pnl IS NOT NULL
        ORDER BY timestamp_open
    """, conn)
    conn.close()

    if len(df) == 0:
        return None

    df["timestamp_open"] = pd.to_datetime(df["timestamp_open"], format="ISO8601", utc=True)
    df["timestamp_close"] = pd.to_datetime(df["timestamp_close"], format="ISO8601", utc=True)

    now = pd.Timestamp.now(tz="UTC")

    # === Build feature dict ===
    features = {}
    features["entry_score"] = entry_score
    features["is_call"] = 1 if option_type == "call" else 0
    features["is_put"] = 1 if option_type == "put" else 0
    features["entry_price"] = entry_price
    features["strike"] = option_strike

    # Time features (assume now is the candidate entry time)
    et_hour = (now.hour - 4) % 24  # UTC to ET approx
    features["hour"] = et_hour
    features["minute"] = now.minute
    features["day_of_week"] = now.dayofweek
    features["is_morning"] = 1 if et_hour < 12 else 0
    features["is_first_30_min"] = 1 if (et_hour == 9 and now.minute < 45) else 0
    features["minutes_since_open"] = max(0, (et_hour - 9) * 60 + now.minute - 30)

    # === Context features (all prior trades) ===
    prior_trades = df[df["pnl"].notna()].copy()

    # Loss streak
    if len(prior_trades) > 0:
        losses = (prior_trades["pnl"] < 0).astype(int).values
        streak = 0
        for v in reversed(losses):
            if v == 1:
                streak += 1
            else:
                break
        features["consecutive_losses"] = streak
    else:
        features["consecutive_losses"] = 0

    # Today's trades (use ET date)
    et_date = (now.tz_convert("America/New_York") if hasattr(now, "tz_convert") else now).date()
    today_trades = prior_trades[
        prior_trades["timestamp_open"].dt.tz_convert("America/New_York").dt.date == et_date
    ]

    features["trades_today_count"] = len(today_trades)
    features["losses_today"] = (today_trades["pnl"] < 0).sum() if len(today_trades) > 0 else 0
    features["wins_today"] = (today_trades["pnl"] > 0).sum() if len(today_trades) > 0 else 0
    features["pnl_today"] = today_trades["pnl"].sum() if len(today_trades) > 0 else 0.0
    features["avg_pnl_today"] = today_trades["pnl"].mean() if len(today_trades) > 0 else 0.0

    # Symbol-specific features
    symbol_history = prior_trades[prior_trades["symbol"] == symbol]
    features["symbol_prior_trades"] = len(symbol_history)
    features["symbol_prior_winrate"] = (
        (symbol_history["pnl"] > 0).mean() if len(symbol_history) > 0 else 0.5
    )
    features["symbol_prior_avg_pnl"] = (
        symbol_history["pnl"].mean() if len(symbol_history) > 0 else 0.0
    )
    features["symbol_prior_total_pnl"] = (
        symbol_history["pnl"].sum() if len(symbol_history) > 0 else 0.0
    )

    if len(symbol_history) > 0:
        last_symbol_trade = symbol_history.iloc[-1]
        last_time = last_symbol_trade["timestamp_close"]
        minutes_since = (now - last_time).total_seconds() / 60
        features["minutes_since_last_symbol_trade"] = minutes_since
        features["last_symbol_trade_was_loss"] = 1 if last_symbol_trade["pnl"] < 0 else 0
    else:
        features["minutes_since_last_symbol_trade"] = 99999
        features["last_symbol_trade_was_loss"] = 0

    # Recent 5 trades
    recent_5 = prior_trades.tail(5)
    if len(recent_5) > 0:
        features["recent_5_avg_pnl"] = recent_5["pnl"].mean()
        features["recent_5_winrate"] = (recent_5["pnl"] > 0).mean()
        features["recent_5_loss_count"] = (recent_5["pnl"] < 0).sum()
    else:
        features["recent_5_avg_pnl"] = 0.0
        features["recent_5_winrate"] = 0.5
        features["recent_5_loss_count"] = 0

    # Convert to DataFrame with correct column order
    feature_df = pd.DataFrame([features])[feature_cols].fillna(0)
    return feature_df


def predict_trade_profitability(symbol, option_type, option_strike, entry_price, entry_score):
    """
    Predict probability this trade will be profitable.

    Returns:
        dict with probability, recommendation (BUY/SKIP), confidence
    """
    try:
        model, metadata = _load_model()
        feature_df = _get_current_context(
            symbol, option_type, option_strike, entry_price, entry_score
        )
        if feature_df is None:
            return {
                "probability": 0.5,
                "recommendation": "SKIP",
                "confidence": "low",
                "reason": "no_training_data"
            }

        prob = float(model.predict_proba(feature_df)[0, 1])

        # Decision thresholds
        if prob >= 0.50:
            rec = "BUY"
            confidence = "high"
        elif prob >= 0.35:
            rec = "BUY"  # lower threshold — model may be useful
            confidence = "medium"
        elif prob >= 0.20:
            rec = "SKIP"
            confidence = "medium"
        else:
            rec = "SKIP"
            confidence = "high"

        return {
            "probability": round(prob, 4),
            "recommendation": rec,
            "confidence": confidence,
            "model_auc": metadata.get("test_auc"),
        }

    except Exception as e:
        return {
            "probability": 0.5,
            "recommendation": "BUY",  # fail-open
            "confidence": "low",
            "reason": f"ml_error: {e}"
        }


if __name__ == "__main__":
    # Test predictions on today's candidates
    print("=" * 60)
    print("ML Predictor — Test Predictions")
    print("=" * 60)

    test_cases = [
        ("NVDA", "call", 220.0, 2.50, 50),
        ("AMZN", "call", 275.0, 2.50, 40),
        ("SPY", "put", 770.0, 1.00, -45),
        ("TSLA", "call", 250.0, 4.00, 35),
        ("MU", "call", 100.0, 25.00, 50),
    ]

    for symbol, opt_type, strike, price, score in test_cases:
        result = predict_trade_profitability(symbol, opt_type, strike, price, score)
        print(f"\n{symbol} {opt_type} ${strike} @ ${price} (score={score}):")
        print(f"  P(profit) = {result['probability']:.2%}")
        print(f"  Recommendation: {result['recommendation']} ({result['confidence']} conf)")
