"""
LLM Decision Layer — uses Ollama (Phi-4-mini) for context-aware decisions.

When the ML gate says BUY but confidence is medium, OR when the ML says
SKIP but the candidate has a strong scanner signal, the LLM gets a chance
to override the ML with a reasoning-based decision.

The LLM sees:
- Recent trade history (last 20)
- Today's P&L
- ML's probability
- Scanner signal score
- Market regime (if known)

Returns a decision with reasoning.
"""
import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from ml_features import get_connection


OLLAMA_URL = "http://192.168.88.7:11434"

sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from llm_models import model_for

# Local model (default) — runs on the GPU card
LOCAL_MODEL = "phi4-mini:3.8b"
# Cloud fallback — used only when the local model is unreachable
CLOUD_MODEL = model_for('decision')
MODEL_NAME = LOCAL_MODEL


def _query_ollama(prompt, model=MODEL_NAME, timeout=10):
    """Send prompt to Ollama and return response text.

    Falls back to CLOUD_MODEL if the local model returns an error or times
    out. The local model is preferred (free, fast, on-GPU); the cloud
    fallback exists so the bot never silently loses the LLM layer.
    """
    for try_model in (model, CLOUD_MODEL) if model == LOCAL_MODEL else (model,):
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": try_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 200,
                        "num_ctx": 4096,
                    }
                },
                timeout=timeout
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as e:
            last_err = e
            continue
    raise last_err


def _get_recent_trades(n=20):
    """Get last N closed trades for context."""
    conn = get_connection()
    df = pd.read_sql_query(f"""
        SELECT id, timestamp_open, symbol, option_type, option_strike,
               entry_price, exit_price, pnl, exit_reason
        FROM trades
        WHERE pnl IS NOT NULL
        ORDER BY timestamp_open DESC
        LIMIT {n}
    """, conn)
    conn.close()
    return df


def _format_trade_history(df):
    """Format trades for the LLM prompt."""
    lines = []
    for _, row in df.iterrows():
        pnl_str = f"${row['pnl']:+.0f}" if row['pnl'] >= 0 else f"-${abs(row['pnl']):.0f}"
        lines.append(
            f"  {row['symbol']} ${row['option_strike']:.0f}{row['option_type'][0].upper()} "
            f"@ ${row['entry_price']:.2f} → ${row['exit_price']:.2f} = {pnl_str} ({row['exit_reason']})"
        )
    return "\n".join(lines)


def _get_today_summary():
    """Today's P&L summary."""
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT COUNT(*) as count,
               SUM(pnl) as total_pnl,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses
        FROM trades
        WHERE DATE(timestamp_close) = DATE('now')
          AND pnl IS NOT NULL
    """, conn)
    conn.close()
    if len(df) == 0 or df.iloc[0]["count"] == 0:
        return "No trades today yet."
    row = df.iloc[0]
    return (
        f"{int(row['count'])} trades: {int(row['wins'])} wins, {int(row['losses'])} losses, "
        f"total P&L ${row['total_pnl']:+.2f}"
    )


def llm_review_candidate(symbol, option_type, option_strike, entry_price, score,
                          ml_prob, ml_recommendation):
    """
    Ask the LLM to review a candidate trade.

    Args:
        symbol: stock ticker
        option_type: 'call' or 'put'
        option_strike: strike price
        entry_price: estimated option price
        score: scanner signal score
        ml_prob: ML model's probability of profitability (0-1)
        ml_recommendation: 'BUY' or 'SKIP'

    Returns:
        dict with:
            - decision: 'BUY' or 'SKIP'
            - reasoning: human-readable explanation
            - confidence: 'high', 'medium', 'low'
    """
    recent_trades = _get_recent_trades(15)
    trade_history = _format_trade_history(recent_trades)
    today_summary = _get_today_summary()

    prompt = f"""You are a trading risk advisor. Decide if we should take this trade.

CANDIDATE:
  {symbol} ${option_strike:.0f} {option_type}
  Entry: ~${entry_price:.2f}
  Scanner score: {score:+d} (threshold ±40)
  ML model says: {ml_recommendation} (P(profit) = {ml_prob:.1%})

TODAY SO FAR:
  {today_summary}

RECENT TRADES (most recent first):
{trade_history}

Reply with EXACTLY this format (one line each):
DECISION: BUY or SKIP
CONFIDENCE: high, medium, or low
REASON: <one short sentence, <100 chars>

Look for: same setup losing repeatedly, time-of-day patterns, drawdown, or strong setup despite recent losses."""

    response = _query_ollama(prompt)

    # Parse response
    decision = "SKIP"
    confidence = "low"
    reason = response[:200]

    for line in response.split("\n"):
        line = line.strip()
        if line.upper().startswith("DECISION:"):
            if "BUY" in line.upper():
                decision = "BUY"
            elif "SKIP" in line.upper():
                decision = "SKIP"
        elif line.upper().startswith("CONFIDENCE:"):
            if "HIGH" in line.upper():
                confidence = "high"
            elif "MEDIUM" in line.upper() or "MED" in line.upper():
                confidence = "medium"
            elif "LOW" in line.upper():
                confidence = "low"
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()[:200]

    return {
        "decision": decision,
        "confidence": confidence,
        "reasoning": reason,
        "raw_response": response[:300],
    }


if __name__ == "__main__":
    print("=" * 60)
    print("LLM Decision Layer — Test")
    print("=" * 60)

    test_cases = [
        ("NVDA", "call", 220.0, 2.50, 50, 0.80, "BUY"),
        ("AMZN", "call", 275.0, 2.27, 40, 0.19, "SKIP"),
        ("MU", "call", 100.0, 24.0, 50, 0.17, "SKIP"),
    ]

    for sym, otype, strike, price, score, ml_prob, ml_rec in test_cases:
        print(f"\n--- Asking LLM about {sym} {otype} ${strike} ---")
        result = llm_review_candidate(sym, otype, strike, price, score, ml_prob, ml_rec)
        print(f"Decision: {result['decision']} ({result['confidence']} confidence)")
        print(f"Reason: {result['reasoning']}")
        print(f"Raw: {result['raw_response'][:200]}")
