#!/bin/bash
# Stop V2 trader. Closes all positions, cancels all orders, then stops services.
set -e

export XDG_RUNTIME_DIR=/run/user/$(id -u)

echo "=== Stopping V2 trader at $(date) ==="

# Stop the daemon first (it runs cleanup on SIGTERM)
if systemctl --user is-active --quiet trader-v2.service; then
    echo "Stopping V2 daemon (will run cleanup: close positions + cancel orders)..."
    systemctl --user stop trader-v2.service
fi

# Then stop the dashboard
if systemctl --user is-active --quiet trader-v2-dashboard.service; then
    echo "Stopping V2 dashboard..."
    systemctl --user stop trader-v2-dashboard.service
fi

echo
echo "V2 trader stopped."
echo
echo "Final state:"
echo "  Daemon:    $(systemctl --user is-active trader-v2.service)"
echo "  Dashboard: $(systemctl --user is-active trader-v2-dashboard.service)"
