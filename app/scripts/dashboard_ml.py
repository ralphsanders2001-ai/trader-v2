"""
ML/LLM Decision Dashboard — for display board monitoring.

Serves a real-time view of:
- Recent trade outcomes (wins/losses)
- ML model decisions (BUY/SKIP with probability)
- LLM reasoning
- Today's P&L
- Win rate over time

Listens on port 8092 (separate from main dashboard on 8091).
Auto-refreshes every 5 seconds via JavaScript.
"""
import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, render_template_string, jsonify

sys.path.insert(0, "/home/ralph/trader-v2/scripts")
from ml_features import get_connection


app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ML Trading Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0a0e1a;
      color: #e0e0e0;
      padding: 20px;
    }
    h1 { color: #4ade80; margin-bottom: 20px; font-size: 1.5em; }
    h2 { color: #60a5fa; margin: 15px 0 10px 0; font-size: 1.1em; }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 15px;
      margin-bottom: 20px;
    }
    .card {
      background: #1a1f2e;
      border: 1px solid #2a3142;
      border-radius: 8px;
      padding: 15px;
    }
    .stat {
      display: flex;
      justify-content: space-between;
      padding: 5px 0;
      border-bottom: 1px solid #2a3142;
    }
    .stat:last-child { border: none; }
    .stat-label { color: #94a3b8; }
    .stat-value { font-weight: 600; }
    .pos { color: #4ade80; }
    .neg { color: #f87171; }
    .neutral { color: #94a3b8; }
    .trade-row {
      display: grid;
      grid-template-columns: 80px 1fr 100px 80px;
      padding: 6px;
      border-bottom: 1px solid #2a3142;
      font-size: 0.85em;
      gap: 8px;
    }
    .symbol { font-weight: 700; }
    .win { background: #064e3b; }
    .loss { background: #7f1d1d; }
    .decision-buy { color: #4ade80; font-weight: 700; }
    .decision-skip { color: #f87171; }
    .small { font-size: 0.75em; color: #64748b; }
    .last-updated {
      position: fixed;
      top: 10px;
      right: 20px;
      font-size: 0.7em;
      color: #64748b;
    }
  </style>
</head>
<body>
  <div class="last-updated" id="lastUpdated">Loading...</div>
  <h1>🧠 ML Trading Decisions</h1>

  <div class="grid">
    <div class="card">
      <h2>Today's Performance</h2>
      <div id="todayStats"></div>
    </div>
    <div class="card">
      <h2>Model Status</h2>
      <div id="modelStatus"></div>
    </div>
  </div>

  <h2>RSI Status (5-min candles)</h2>
  <div class="card">
    <div id="rsiStatus"></div>
  </div>

  <h2>Recent Decisions (ML + LLM)</h2>
  <div class="card">
    <div id="recentDecisions"></div>
  </div>

  <h2>Recent Trades</h2>
  <div class="card">
    <div id="recentTrades"></div>
  </div>

  <script>
    function fmt(n) { return n.toFixed(2); }
    function pct(n) { return (n * 100).toFixed(1) + '%'; }
    function pnl(v) {
      if (v >= 0) return '<span class="pos">+$' + fmt(v) + '</span>';
      return '<span class="neg">$' + fmt(v) + '</span>';
    }

    async function refresh() {
      try {
        const resp = await fetch('/api/state');
        const data = await resp.json();

        // Today
        let today = data.today;
        document.getElementById('todayStats').innerHTML = `
          <div class="stat"><span class="stat-label">Trades:</span><span>${today.trades}</span></div>
          <div class="stat"><span class="stat-label">Wins:</span><span class="pos">${today.wins}</span></div>
          <div class="stat"><span class="stat-label">Losses:</span><span class="neg">${today.losses}</span></div>
          <div class="stat"><span class="stat-label">Win rate:</span><span>${today.trades > 0 ? pct(today.wins/today.trades) : '—'}</span></div>
          <div class="stat"><span class="stat-label">Total P&L:</span><span>${pnl(today.total_pnl)}</span></div>
          <div class="stat"><span class="stat-label">Avg win:</span><span class="pos">$${fmt(today.avg_win)}</span></div>
          <div class="stat"><span class="stat-label">Avg loss:</span><span class="neg">$${fmt(today.avg_loss)}</span></div>
        `;

        // Model
        let model = data.model;
        document.getElementById('modelStatus').innerHTML = `
          <div class="stat"><span class="stat-label">Model:</span><span>${model.type}</span></div>
          <div class="stat"><span class="stat-label">Trained on:</span><span>${model.train_size} trades</span></div>
          <div class="stat"><span class="stat-label">Test AUC:</span><span class="pos">${model.test_auc}</span></div>
          <div class="stat"><span class="stat-label">CV AUC:</span><span>${model.cv_auc}</span></div>
          <div class="stat"><span class="stat-label">ML Gate:</span><span>${model.ml_enabled ? 'ON' : 'OFF'}</span></div>
          <div class="stat"><span class="stat-label">LLM Gate:</span><span>${model.llm_enabled ? 'ON' : 'OFF'}</span></div>
        `;

        // Decisions
        let dec = data.decisions.map(d => `
          <div class="trade-row">
            <span class="symbol">${d.symbol}</span>
            <span class="small">${d.option} ${d.reason}</span>
            <span class="${d.action === 'BUY' ? 'decision-buy' : 'decision-skip'}">${d.action}</span>
            <span class="small">${d.prob}</span>
          </div>
        `).join('') || '<div class="small">No decisions yet</div>';
        document.getElementById('recentDecisions').innerHTML = dec;

        // Trades
        let trades = data.trades.map(t => `
          <div class="trade-row ${t.pnl > 0 ? 'win' : t.pnl < 0 ? 'loss' : ''}">
            <span class="symbol">${t.symbol} $${t.strike}${t.type[0].toUpperCase()}</span>
            <span class="small">${t.entry} → ${t.exit} (${t.reason})</span>
            <span>${pnl(t.pnl)}</span>
            <span class="small">${t.min_held}m</span>
          </div>
        `).join('') || '<div class="small">No trades today</div>';
        document.getElementById('recentTrades').innerHTML = trades;

        // RSI Status
        let rsiData = data.rsi || {};
        let rsiHtml = '<div class="grid">';
        Object.keys(rsiData).sort().forEach(sym => {
          let r = rsiData[sym];
          if (!r || r.rsi === null) return;
          let color = 'neutral';
          if (r.signal === 'overbought' || r.signal === 'extreme_overbought') color = 'neg';
          else if (r.signal === 'oversold' || r.signal === 'extreme_oversold') color = 'pos';
          else if (r.signal === 'neutral') color = 'pos';
          let div = r.divergence ? ` <span class="small">[${r.divergence}]</span>` : '';
          rsiHtml += `
            <div class="stat">
              <span class="stat-label">${sym}</span>
              <span class="${color}">${r.rsi} (${r.signal})${div}</span>
            </div>
          `;
        });
        rsiHtml += '</div>';
        document.getElementById('rsiStatus').innerHTML = rsiHtml || '<div class="small">No RSI data</div>';

        document.getElementById('lastUpdated').textContent =
          'Updated ' + new Date().toLocaleTimeString();
      } catch (e) {
        document.getElementById('lastUpdated').textContent = 'Error: ' + e;
      }
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


def get_today_stats():
    """Today's trading summary."""
    conn = get_connection()
    df = conn.execute("""
        SELECT COUNT(*) as trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
               COALESCE(SUM(pnl), 0) as total_pnl,
               COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
               COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl END), 0) as avg_loss
        FROM trades
        WHERE DATE(timestamp_close) = DATE('now')
          AND pnl IS NOT NULL
    """).fetchone()
    conn.close()
    return {
        "trades": df[0] or 0,
        "wins": df[1] or 0,
        "losses": df[2] or 0,
        "total_pnl": float(df[3] or 0),
        "avg_win": float(df[4] or 0),
        "avg_loss": float(df[5] or 0),
    }


def get_model_status():
    """Load model metadata."""
    meta_path = Path("/home/ralph/trader-v2/models/ml_metadata_v1.json")
    if not meta_path.exists():
        return {
            "type": "Not trained",
            "train_size": 0,
            "test_auc": "—",
            "cv_auc": "—",
            "ml_enabled": False,
            "llm_enabled": False,
        }
    with open(meta_path) as f:
        meta = json.load(f)

    # Lazy-load config.py via importlib to avoid sys.path confusion
    import importlib.util
    config_path = Path("/home/ralph/trader-v2/config.py")
    spec = importlib.util.spec_from_file_location("config", config_path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)

    return {
        "type": meta.get("model_type", "XGBClassifier"),
        "train_size": meta.get("train_size", 0),
        "test_auc": f"{meta.get('test_auc', 0):.3f}",
        "cv_auc": f"{meta.get('cv_auc_mean', 0):.3f} ±{meta.get('cv_auc_std', 0):.3f}",
        "ml_enabled": getattr(cfg, "USE_ML_GATE", True),
        "llm_enabled": getattr(cfg, "USE_LLM_GATE", True),
    }


def get_recent_decisions(n=20):
    """Recent ML/LLM decisions from signals table."""
    conn = get_connection()
    df = conn.execute(f"""
        SELECT symbol, direction, status, rejection_reason, timestamp
        FROM signals
        ORDER BY id DESC
        LIMIT {n}
    """).fetchall()
    conn.close()

    decisions = []
    for row in df:
        symbol, direction, status, reason, ts = row
        action = "BUY" if status in ("taken", "filled") else "SKIP"
        reason_text = reason or ""
        if reason_text.startswith("ml_skip"):
            reason_text = f"ML: {reason_text}"
        elif reason_text.startswith("llm_skip"):
            reason_text = f"LLM: {reason_text}"
        elif reason_text == "no_contract":
            reason_text = "no contract"

        # Parse probability from ml_skip
        prob = "—"
        if reason_text.startswith("ML: ml_skip_p"):
            try:
                prob = reason_text.split("p")[1][:4]
            except Exception:
                pass

        decisions.append({
            "symbol": symbol,
            "option": direction,
            "action": action,
            "reason": reason_text[:50],
            "prob": prob,
        })
    return decisions


def get_recent_trades(n=15):
    """Recent closed trades."""
    conn = get_connection()
    df = conn.execute(f"""
        SELECT symbol, option_type, option_strike, entry_price, exit_price,
               pnl, exit_reason,
               ROUND((julianday(timestamp_close) - julianday(timestamp_open)) * 24 * 60, 1) as min_held
        FROM trades
        WHERE pnl IS NOT NULL
          AND DATE(timestamp_close) = DATE('now')
        ORDER BY timestamp_close DESC
        LIMIT {n}
    """).fetchall()
    conn.close()

    trades = []
    for row in df:
        trades.append({
            "symbol": row[0],
            "type": row[1],
            "strike": float(row[2]),
            "entry": float(row[3]),
            "exit": float(row[4]),
            "pnl": float(row[5]),
            "reason": row[6] or "",
            "min_held": float(row[7] or 0),
        })
    return trades


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/state")
def api_state():
    # Get current RSI for all open positions + watchlist
    rsi_status = {}
    try:
        import sys
        sys.path.insert(0, "/home/ralph/trader-v2/scripts")
        from rsi_exit import get_rsi_status

        conn = get_connection()
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM positions"
        ).fetchall()]
        conn.close()

        # Also check watchlist symbols
        watchlist = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MU", "INTC", "AMD"]
        for s in set(symbols + watchlist):
            rsi_status[s] = get_rsi_status(s)
    except Exception as e:
        rsi_status = {"error": str(e)}

    return jsonify({
        "today": get_today_stats(),
        "model": get_model_status(),
        "decisions": get_recent_decisions(15),
        "trades": get_recent_trades(15),
        "rsi": rsi_status,
    })


@app.route("/api/rsi/<symbol>")
def api_rsi(symbol):
    """Get current RSI for a specific symbol."""
    try:
        from rsi_exit import get_rsi_status
        return jsonify(get_rsi_status(symbol.upper()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()

    # Ensure we're running from scripts dir so config.py is importable
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(scripts_dir)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    print(f"Starting ML dashboard on port {args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
