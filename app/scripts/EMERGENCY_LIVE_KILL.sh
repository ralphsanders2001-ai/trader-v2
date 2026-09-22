#!/bin/bash
# EMERGENCY: kill ALL live trading immediately.
# This stops everything that could place real orders on Robinhood.
# Paper bot is left alone (it's harmless).
# To UNDO: see /home/ralph/trader-v2/scripts/EMERGENCY_LIVE_RESTORE.sh

set -e

echo "=== KILLING ALL LIVE TRADING ==="
echo

# 1. Stop all V2 live trading services
for svc in trader-v2-live.service; do
  systemctl --user stop $svc 2>/dev/null && echo "✓ stopped $svc" || echo "  $svc already stopped"
  systemctl --user disable $svc 2>/dev/null && echo "✓ disabled $svc" || echo "  $svc already disabled"
done

# 2. Kill V1 legacy monitor (the V1 system that sold positions without permission)
#    - The service file was deleted and moved to v1/services/ for reference
pkill -9 -f monitor_active.py 2>/dev/null && echo "✓ killed any monitor_active.py" || echo "  no monitor_active.py running"
pkill -9 -f "auto_trade.py" 2>/dev/null && echo "✓ killed any auto_trade.py" || echo "  no auto_trade.py running"

# 3. Kill the robinhood-for-agents MCP server (blocks all MCP order tools)
pkill -9 -f "robinhood-for-agents" 2>/dev/null && echo "✓ killed robinhood-for-agents MCP" || echo "  no MCP server running"

# 4. Disable the MCP server in config so it doesn't restart with Hermes
/usr/bin/python3 -c "
path = '/home/ralph/.hermes/config.yaml'
with open(path) as f:
    content = f.read()
old = '    command: /home/ralph/.bun/bin/robinhood-for-agents\n    enabled: true'
new = '    command: /home/ralph/.bun/bin/robinhood-for-agents\n    enabled: false  # EMERGENCY: live trading disabled'
if old in content:
    content = content.replace(old, new)
    with open(path, 'w') as f:
        f.write(content)
    print('✓ hermes config: MCP disabled')
else:
    print('  hermes config: MCP already disabled')
" 2>/dev/null

echo
echo "=== VERIFICATION ==="
echo
echo "V2 live service:        $(systemctl --user is-active trader-v2-live.service 2>&1)"
echo "V1 monitor service:     should be DELETED"
systemctl --user status robinhood-live-monitor.service 2>&1 | head -1 || echo "  ✓ gone"
echo
echo "Processes that could place live orders:"
ps -ef | grep -iE "monitor_active|auto_trade|robinhood-for-agents" | grep -v grep | head -5
echo
echo "=== PAPER BOT (still running, doesn't touch real money) ==="
systemctl --user is-active trader-v2-paper.service
echo
echo "=== V1 ARCHIVE (kept for reference, not running) ==="
echo "  /home/ralph/trader-v2/v1/  (27 files: scripts, models, services, backups, legacy)"
echo
echo "DONE. Live trading is OFF. Use EMERGENCY_LIVE_RESTORE.sh to bring it back."
