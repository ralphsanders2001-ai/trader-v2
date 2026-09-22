# Trader V2

Live options trading system with internal scheduling, Robinhood API integration,
and a dashboard for monitoring and control.

## Quick Start (Tomorrow Morning)

### Auto-start (already configured)

The V2 trader has TWO cron jobs already scheduled:
- **v2-trader-startup** (job `5fab755ba687`) — fires weekdays at 9:25 AM ET
- **v2-trader-shutdown** (job `37bd4184e151`) — fires weekdays at 4:05 PM ET (with cleanup)

### Manual start (recommended for first time)

```bash
# 1. Install the systemd service files (already done)
ls ~/.config/systemd/user/trader-v2*.service
# Should list 2 files

# 2. Start the daemon (will trigger 2FA)
bash /home/ralph/trader-v2/start_trader_v2.sh

# 3. Open the dashboard and approve 2FA
#    http://192.168.88.7:8091
#    - Check phone for Robinhood push notification
#    - Approve
#    - Enter the verification code on the dashboard

# 4. After 2FA approval, the daemon runs all day
#    - 9:30 AM: market opens, scanning begins
#    - 9:30-9:45 AM: no-trade window (15 min)
#    - 3:55 PM: force-close all options (EOD flatten)
#    - 4:00 PM: market closes, daemon keeps running
#    - 4:05 PM: auto-shutdown cron runs (clean stop)

# 5. To stop cleanly (closes positions, cancels orders):
bash /home/ralph/trader-v2/stop_trader_v2.sh
```

## Architecture

```
/home/ralph/trader-v2/
├── config.py              # ALL tunables (percentages, times, thresholds)
├── trader-v2.service      # systemd unit for daemon
├── trader-v2-dashboard.service
├── data/trader.db         # SQLite database
├── logs/daemon.log        # daemon output
├── templates/             # Flask templates
│   ├── index.html         # Live positions + SELL NOW
│   ├── settings.html      # Config editor
│   └── auth.html          # 2FA prompt
└── scripts/
    ├── daemon.py          # Main orchestrator (internal scheduler)
    ├── dashboard.py       # Flask app on port 8091
    ├── database.py        # SQLite schema + helpers
    ├── config_loader.py   # Hot-reload from dashboard
    ├── robinhood_client.py # Robinhood API wrapper (2FA + pickle)
    ├── scanner.py         # Top movers + signal generation
    ├── executor.py        # Order placement + position tracking
    └── indicators.py      # RSI, MACD, BBands on 5m candles
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Single daemon, no cron** | All timing internal — change intervals without editing crons |
| **Poll tiers 5/10/15s** | No 30s default. Fast on volatile, slow on calm |
| **5m candles only** | User-specified. Each candle = 5 min. No 1m/2m ever |
| **Sliding-scale loss cap** | min(20% of position, $40 absolute) — whichever tighter |
| **Tier-based profit targets** | volatile=10%, standard=20%, calm=25% |
| **Progressive cooldown** | 1st loss=30min, 2nd=60min, 3rd=BLOCKED rest of day |
| **$250 circuit breaker** | Daily cumulative loss → close all + halt |
| **Win-streak sizing** | 3 wins in a row → 1.5x size |
| **Hot-reload config** | Dashboard edits → config.py → daemon picks up in 5s |
| **Graceful shutdown** | SIGTERM → cancel orders + close positions → exit |

## Default Config (key values)

```python
LOSS_CAP_PCT = 0.20          # 20% of position value
LOSS_CAP_DOLLAR = 40.0       # $40 absolute max
MAX_DAILY_LOSS = 250.0       # circuit breaker
POLL_TIERS = {5, 10, 15}     # seconds per tier
TIER_PROFIT_TARGETS = {10%, 20%, 25%}
COOLDOWN_FIRST_LOSS = 30     # minutes
COOLDOWN_SECOND_LOSS = 60    # minutes
NO_TRADE_WINDOW = "9:30-9:45" # first 15 min after open
PRIMARY_TIMEFRAME = "5m"     # each candle = 5 minutes
MAX_OPEN_POSITIONS = 3
MAX_BUYS_PER_DAY = 20
```

## What's NOT Built Yet

- LLM advisor (config has OLLAMA_HOST/MODEL but no advisor module yet)
- To-do list module (planned, schema in DB)
- Daily summary rollup (schema exists, code doesn't)
- Detailed position P&L history graph (just current state)

## Tomorrow Morning Operational Checklist

Before 9:25 AM ET (auto-start fires):
- [x] Systemd services installed (✓ done)
- [x] Auto-start cron (✓ done, 9:25 AM weekdays)
- [x] Auto-shutdown cron (✓ done, 4:05 PM weekdays with cleanup)
- [x] DB initialized (✓ done, 8 tables)
- [ ] **Wait for 429 rate limit to expire** (Robinhood auth rate limit was hit tonight)
- [ ] Approve Robinhood push on phone when it arrives
- [ ] Enter verification code on dashboard

If 429 is still active in the morning:
- Wait 5-10 minutes and try `bash /home/ralph/trader-v2/start_trader_v2.sh` again
- Or restart just the daemon: `systemctl --user restart trader-v2.service`

## State

- V1 (robinhood-trainer) is DISABLED, daemon stopped, all crons paused
- V1 files preserved at `/home/ralph/robinhood-trainer/` for reference
- V1 backups at `/home/ralph/trader-v2-backups/`
- Will remove V1 only after V2 has been live-tested for ≥1 trading day
