#!/bin/bash
# Switch ai.hermes.gateway PYTHONPATH to a new release with auto-rollback. Run detached.
NEW="$1"; P="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"; U=$(id -u)
LOG="$HOME/.hermes/logs/gateway-switch.log"; BK="$HOME/.hermes/backups/ai.hermes.gateway.plist.$(date +%Y%m%d-%H%M%S)"
exec >>"$LOG" 2>&1; echo "== $(date) switch to $NEW"
# Every candidate must retain the tested owner-board path before downtime.
CONTROLLER=$(awk '/^REL=/{sub(/^REL=/, ""); print; exit}' "$HOME/.hermes/scripts/hermes-kanban-guarded")
/Users/Chris/.hermes/releases/hermes-agent-20260925-private-delivery-proof-v57/.venv/bin/python "$HOME/.hermes/scripts/board-owner-preflight.py" --gateway "$NEW" --controller "$CONTROLLER" || exit 1
cp "$P" "$BK"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:PYTHONPATH $NEW" "$P" || exit 1
launchctl bootout gui/$U/ai.hermes.gateway
for i in $(seq 1 80); do launchctl print gui/$U/ai.hermes.gateway >/dev/null 2>&1 || break; sleep 3; done
for t in 1 2 3 4 5; do launchctl bootstrap gui/$U "$P" && break; echo "bootstrap retry $t"; sleep 5; done
for i in $(seq 1 30); do sleep 3; curl -sf -m3 http://127.0.0.1:8642/health | grep -q '"status": "ok"' && { echo "healthy after $((i*3))s"; exit 0; }; done
echo "UNHEALTHY -> rollback"; cp "$BK" "$P"; launchctl bootout gui/$U/ai.hermes.gateway; sleep 3; launchctl bootstrap gui/$U "$P"
sleep 20; curl -s -m3 http://127.0.0.1:8642/health | head -c 80; echo " rolled back"
