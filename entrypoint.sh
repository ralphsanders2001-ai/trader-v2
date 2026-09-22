#!/bin/bash
# Trader V2 container entrypoint — paper daemon + dashboard
set -e
export TRADER_CONFIG_FILE=config.py   # paper config (MODE=paper)
cd /home/ralph/trader-v2

# daemon.py: scheduler/executor (survives login failure, waits for MFA via dashboard)
python3 scripts/daemon.py &
DAEMON_PID=$!

# dashboard.py: flask UI on :8091. Import the module and run the app directly —
# its __main__ does a blocking startup login (waits on RH push). With a valid
# pickle in /home/ralph/.tokens routes auto-login on demand; MFA flow is via UI.
python3 -c "import sys; sys.path.insert(0,'/home/ralph/trader-v2'); sys.path.insert(0,'/home/ralph/trader-v2/scripts'); import config; import dashboard; dashboard.app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False)" &
DASH_PID=$!

trap 'kill $DAEMON_PID $DASH_PID 2>/dev/null' TERM INT
wait -n $DAEMON_PID $DASH_PID
wait
