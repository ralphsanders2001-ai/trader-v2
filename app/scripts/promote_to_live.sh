#!/bin/bash
# Promote paper settings to live.
# Ralph's "flick of a switch" — no investigation needed.
# Usage: bash /home/ralph/trader-v2/scripts/promote_to_live.sh

set -e

PAPER="/home/ralph/trader-v2/config.py"
LIVE="/home/ralph/trader-v2/config_live.py"
BACKUP_DIR="/home/ralph/trader-v2/backups"

# 1. Backup current live config
mkdir -p "$BACKUP_DIR"
TS=$(date +%Y-%m-%d_%H%M%S)
cp "$LIVE" "$BACKUP_DIR/${TS}_config_live_pre_promote.py"
echo "Backed up live config to $BACKUP_DIR/${TS}_config_live_pre_promote.py"

# 2. Copy paper-tuned settings to live config.
#    These are the keys we actually tune on paper; everything else stays.
python3 - <<'PY'
import re
with open("/home/ralph/trader-v2/config.py") as f:
    paper = f.read()
with open("/home/ralph/trader-v2/config_live.py") as f:
    live = f.read()

# Keys the user iterates on paper — sync these to live when ready.
KEYS = [
    "ML_SKIP_THRESHOLD",
    "LOSS_CAP_PCT_NEW",
    "MAX_PROFIT_DOLLAR",
    "TAKE_PROFIT_DOLLAR",
    "EOD_CUTOFF_HHMM",
    "MAX_HOLD_DAYS_3DTE",
    "WATCHLIST",
]

def patch(text, key, new_value):
    pattern = rf"^({key}\s*=\s*).*?(?=#|\n|$)"
    return re.sub(pattern, rf"\g<1>{new_value}", text, flags=re.MULTILINE)

# Extract values from paper
for key in KEYS:
    m = re.search(rf"^{key}\s*=\s*(.+?)(?=\n|$)", paper, re.MULTILINE | re.DOTALL)
    if not m:
        print(f"  WARN: {key} not found in paper config")
        continue
    paper_value = m.group(1).rstrip()

    # For WATCHLIST (list), we need to extract the whole list block
    if key == "WATCHLIST":
        m_live = re.search(r"^WATCHLIST\s*=\s*\[(.*?)\]", live, re.MULTILINE | re.DOTALL)
        if m_live:
            live = live.replace(m_live.group(0), f"WATCHLIST = {paper_value}")
            print(f"  set {key} (list)")
        continue

    # For scalars
    m_live = re.search(rf"^{key}\s*=\s*.*?(?=\n|$)", live, re.MULTILINE)
    if m_live:
        live = live.replace(m_live.group(0), f"{key} = {paper_value}")
        print(f"  set {key} = {paper_value}")

# Force MODE = live in live config (don't let paper override)
live = re.sub(r'^MODE\s*=\s*".+?"', 'MODE = "live"', live, flags=re.MULTILINE)

with open("/home/ralph/trader-v2/config_live.py", "w") as f:
    f.write(live)
print("\nPromoted paper settings to live config.")
PY

# 3. Show the diff
echo
echo "=== Diff (paper vs live) ==="
diff "$PAPER" "$LIVE" | head -50

# 4. Restart live bot if it was running
echo
echo "=== Live service state ==="
systemctl --user is-active trader-v2-live.service || true

echo
echo "Done. To start live: systemctl --user start trader-v2-live.service"
echo "To stop paper first: systemctl --user stop trader-v2-paper.service"
