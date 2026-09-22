# Trader V2

Paper-trading options bot for Robinhood with an LLM-assisted decision layer,
a Flask dashboard for monitoring and control, and full audit trails.

> **Status:** personal project, shared for feedback. **Paper mode only by
> default** — nothing here places live orders unless you explicitly switch
> config. Options trading involves substantial risk; this is not financial
> advice. Use at your own risk.

## What it does

- **Scanner** polls watchlist symbols and builds candidate signals
  (indicators, regime, multi-factor checklist, policy overlays).
- **LLM layer** optionally consults a local/cloud LLM for second opinions
  (`llm_decide.py` — works with Ollama or cloud models).
- **Executor** places paper trades via the Robinhood API, honoring
  configurable knobs: buy limit offset, take-profit exits, hold times,
  per-symbol cooldowns, daily loss caps.
- **Dashboard** (`:8091`) shows positions, fills with prices + exit reasons,
  recent signals with rejection reasons, settings (hot-reloaded), backtests,
  and a go-live gate.
- **ML sidecar** (`ml_*.py`) feature engineering + XGBoost models for
  signal quality ranking, with drift detection and deflated Sharpe stats.

## Layout

```
app/
├── config.py            # ALL tunables — hot-reloaded every 5s by daemon
├── config_live.py       # live-mode tunables (separate risk caps)
├── backtest_real_v2.py  # backtester over historical candles
├── scripts/
│   ├── daemon.py        # main trading loop
│   ├── dashboard.py     # Flask UI + settings API
│   ├── executor.py      # order placement / exits
│   ├── scanner.py       # signal generation
│   ├── indicators.py    # RSI/MACD/ADX etc.
│   ├── robinhood_client.py  # RH session + 2FA handling
│   ├── database.py      # SQLite schema + queries
│   └── ...
├── templates/           # dashboard HTML
└── v1/                  # legacy trainer scripts (kept for reference)
```

## Quick start (Docker)

```bash
git clone <repo-url> trader-v2 && cd trader-v2

# 1. Credentials — create your own from the template:
cp app/rh_credentials.py.example app/rh_credentials.py
$EDITOR app/rh_credentials.py

# 2. Token dir (daemon writes the RH session pickle here):
mkdir -p tokens && chmod 700 tokens

# 3. Run:
docker compose up -d
# Dashboard: http://localhost:8091
```

First login triggers Robinhood 2FA — approve the push / enter the code on the
dashboard. The session pickle lands in `tokens/`.

## Quick start (bare metal)

```bash
pip install -r requirements.txt
export TRADER_CONFIG_FILE=config.py    # paper mode
python app/scripts/daemon.py &         # trading loop
python app/scripts/dashboard.py &      # UI on :8091
```

## Configuration

Everything tunable lives in `app/config.py` (scan interval, poll tiers,
take-profit tiers, hold times, cooldowns, daily loss caps, buy-limit offset...).
The daemon hot-reloads it every 5 seconds, and the dashboard's Settings page
writes changes directly to the file.

## Paper vs live

- Paper mode (`TRADER_CONFIG_FILE=config.py`) simulates fills and is the default.
- Live config (`config_live.py`) has separate, tighter risk caps.
- The dashboard exposes a **go-live gate** — trading stays paper until it is
  explicitly flipped.

## Feedback

Open a GitHub Issue with:
- what you ran (paper/live, symbols, timeframe),
- what you expected vs. the dashboard/DB showing,
- daemon log excerpt if relevant.

## License

MIT — see [LICENSE](LICENSE).