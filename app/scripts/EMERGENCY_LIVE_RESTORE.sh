#!/bin/bash
# Undo EMERGENCY_LIVE_KILL.sh — enables live trading again.
# USE WITH CARE. This will start placing real orders on Robinhood.

set -e

echo "=== RESTORING LIVE TRADING ==="
echo

# 1. Re-enable the MCP server
/usr/bin/python3 -c "
path = '/home/ralph/.hermes/config.yaml'
with open(path) as f:
    content = f.read()
old = '    command: /home/ralph/.bun/bin/robinhood-for-agents\n    enabled: false  # EMERGENCY: live trading disabled'
new = '    command: /home/ralph/.bun/bin/robinhood-for-agents\n    enabled: true'
if old in content:
    content = content.replace(old, new)
    with open(path, 'w') as f:
        f.write(content)
    print('✓ hermes MCP re-enabled')
else:
    print('  hermes MCP already enabled')
"

# 2. Re-enable and start the live bot
systemctl --user enable trader-v2-live.service
echo "✓ trader-v2-live.service enabled"

# 3. Restart hermes-gateway to pick up the MCP server
systemctl --user restart hermes-gateway.service
echo "✓ hermes-gateway restarted"

# 4. Start the live bot
systemctl --user start trader-v2-live.service
echo "✓ trader-v2-live.service started"

echo
echo "DONE. Live trading is back ON."
