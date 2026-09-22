"""
ML Feature Extractor for Trading Decisions.

Extracts features from historical trades to train an XGBoost model that
predicts whether a new trade will be profitable.

Features:
- Entry signal score (from scanner)
- Time of day (morning vs afternoon)
- Day of week
- Direction (call vs put)
- Recent loss streak (in current day)
- Total losses today
- Total P&L today (running)
- Market regime (SPY/QQQ trend)
- Volatility (VIX)
- Same symbol cooldown time
- Recent symbol performance (last 5 trades)
- Bid-ask spread at entry
- Volume ratio
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = "/home/ralph/trader-v2/data/trader_paper.db"


def get_connection():
    """Open SQLite connection to trader DB."""
    return sqlite3.connect(DB_PATH)


def extract_features_for_trade(trade_row, all_trades_df):
    """
    Extract features for a single closed trade.
    Returns a dict of feature_name -> value.
    """
    features = {}

    # === Entry-level features ===
    features["entry_score"] = trade_row.get("entry_signal_score", 0) or 0
    features["is_call"] = 1 if trade_row["option_type"] == "call" else 0
    features["is_put"] = 1 if trade_row["option_type"] == "put" else 0
    features["entry_price"] = trade_row["entry_price"]

    # === Time features ===
    open_time = pd.to_datetime(trade_row["timestamp_open"])
    features["hour"] = open_time.hour
    features["minute"] = open_time.minute
    features["day_of_week"] = open_time.dayofweek
    features["is_morning"] = 1 if open_time.hour < 12 else 0
    features["is_first_30_min"] = 1 if (open_time.hour == 9 and open_time.minute < 45) else 0
    features["minutes_since_open"] = (open_time.hour - 9) * 60 + open_time.minute - 30
    features["minutes_since_open"] = max(0, features["minutes_since_open"])

    # === Strike vs price (proxy for moneyness) ===
    features["strike"] = trade_row["option_strike"]

    # === Prior context features (look at all trades before this one) ===
    prior_trades = all_trades_df[
        (all_trades_df["timestamp_open"] < trade_row["timestamp_open"]) &
        (all_trades_df["pnl"].notna())
    ].copy()

    # Sort by time
    prior_trades = prior_trades.sort_values("timestamp_open")

    # === Recent loss streak ===
    if len(prior_trades) > 0:
        losses = (prior_trades["pnl"] < 0).astype(int).values
        # Count consecutive losses from the end
        streak = 0
        for v in reversed(losses):
            if v == 1:
                streak += 1
            else:
                break
        features["consecutive_losses"] = streak
    else:
        features["consecutive_losses"] = 0

    # === Trades today (before this one) ===
    trade_date = open_time.date()
    today_trades = prior_trades[prior_trades["timestamp_open"].dt.date == trade_date]

    features["trades_today_count"] = len(today_trades)
    features["losses_today"] = (today_trades["pnl"] < 0).sum() if len(today_trades) > 0 else 0
    features["wins_today"] = (today_trades["pnl"] > 0).sum() if len(today_trades) > 0 else 0
    features["pnl_today"] = today_trades["pnl"].sum() if len(today_trades) > 0 else 0.0
    features["avg_pnl_today"] = today_trades["pnl"].mean() if len(today_trades) > 0 else 0.0

    # === Symbol-specific features ===
    symbol = trade_row["symbol"]
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

    # Last trade on this symbol
    if len(symbol_history) > 0:
        last_symbol_trade = symbol_history.iloc[-1]
        last_time = pd.to_datetime(last_symbol_trade["timestamp_close"])
        minutes_since = (open_time - last_time).total_seconds() / 60
        features["minutes_since_last_symbol_trade"] = minutes_since
        features["last_symbol_trade_was_loss"] = 1 if last_symbol_trade["pnl"] < 0 else 0
    else:
        features["minutes_since_last_symbol_trade"] = 99999
        features["last_symbol_trade_was_loss"] = 0

    # === Recent 5 trades features ===
    recent_5 = prior_trades.tail(5)
    if len(recent_5) > 0:
        features["recent_5_avg_pnl"] = recent_5["pnl"].mean()
        features["recent_5_winrate"] = (recent_5["pnl"] > 0).mean()
        features["recent_5_loss_count"] = (recent_5["pnl"] < 0).sum()
    else:
        features["recent_5_avg_pnl"] = 0.0
        features["recent_5_winrate"] = 0.5
        features["recent_5_loss_count"] = 0

    # === Target (did this trade win?) ===
    features["target"] = 1 if trade_row["pnl"] > 0 else 0
    features["actual_pnl"] = trade_row["pnl"]

    # === Holding duration (for context only, not used at prediction time) ===
    if trade_row.get("timestamp_close"):
        close_time = pd.to_datetime(trade_row["timestamp_close"])
        features["hold_minutes"] = (close_time - open_time).total_seconds() / 60
    else:
        features["hold_minutes"] = 0

    return features


def extract_all_features():
    """Extract features for all closed trades in the DB."""
    conn = get_connection()

    # Pull all closed trades
    df = pd.read_sql_query("""
        SELECT id, timestamp_open, timestamp_close, mode, symbol, option_type,
               option_strike, option_expiry, entry_price, exit_price, pnl,
               exit_reason, entry_signal_score
        FROM trades
        WHERE pnl IS NOT NULL
          AND entry_signal_score IS NOT NULL
        ORDER BY timestamp_open
    """, conn)
    conn.close()

    print(f"Loaded {len(df)} closed trades with signal scores")

    if len(df) == 0:
        print("ERROR: No trades with entry_signal_score found")
        return None

    # Convert timestamp columns to datetime BEFORE iterating
    df["timestamp_open"] = pd.to_datetime(df["timestamp_open"], format="ISO8601", utc=True)
    df["timestamp_close"] = pd.to_datetime(df["timestamp_close"], format="ISO8601", utc=True)

    # Extract features for each trade
    feature_rows = []
    for idx, row in df.iterrows():
        try:
            f = extract_features_for_trade(row, df)
            f["trade_id"] = row["id"]
            f["symbol"] = row["symbol"]
            feature_rows.append(f)
        except Exception as e:
            print(f"Failed on trade {row['id']}: {e}")

    features_df = pd.DataFrame(feature_rows)
    return features_df


if __name__ == "__main__":
    print("=" * 60)
    print("ML Feature Extractor")
    print("=" * 60)
    features = extract_all_features()
    if features is not None and len(features) > 0:
        print(f"\nExtracted features for {len(features)} trades")
        print(f"Win rate: {features['target'].mean():.2%}")
        print(f"Total P&L: ${features['actual_pnl'].sum():.2f}")
        print(f"\nFeature columns ({len(features.columns)}):")
        for col in features.columns:
            print(f"  {col}")
