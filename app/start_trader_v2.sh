#!/bin/bash
# Start V2 trader for the day. Run before market open (before 9:30 AM ET).
# - Idempotent (safe to re-run)
# - Starts daemon + dashboard systemd services
set -e

export XDG_RUNTIME_DIR=/run/user/$(id -u)

echo "=== Starting V2 trader for $(date) ==="

# Start daemon
if ! systemctl --user is-active --quiet trader-v2.service; then
    echo "Starting V2 daemon..."
    systemctl --user start trader-v2.service
else
    echo "V2 daemon already running."
fi

# Start dashboard
if ! systemctl --user is-active --quiet trader-v2-dashboard.service; then
    echo "Starting V2 dashboard..."
    systemctl --user start trader-v2-dashboard.service
else
    echo "V2 dashboard already running."
fi

sleep 3

echo
echo "=== V2 trader started ==="
echo "Daemon:       $(systemctl --user is-active trader-v2.service)"
echo "Dashboard:    $(systemctl --user is-active trader-v2-dashboard.service)"
echo "URL:          http://192.168.88.7:8091"
echo
echo "If status shows 'inactive' (failed), check:"
echo "  journalctl --user -u trader-v2.service -n 50"
echo
echo "If waiting for 2FA, visit the dashboard to approve and submit the code."
