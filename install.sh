#!/bin/bash
# Install agent-bridge v2 on this Mac, or roll it back. Idempotent; never deletes: anything replaced is moved aside.
#
#   ./install.sh            install (or repair) and print the resulting state
#   ./install.sh rollback   restore v1: ~/bin/agent-bridge.v1 back in place, extension and pump job removed
#
# Installs: ~/bin/agent-bridge -> src/agent_bridge.py (v1 moved to ~/bin/agent-bridge.v1),
#           ~/.omp/agent/extensions/agent-bridge.ts -> extension/agent-bridge.ts,
#           ~/Library/LaunchAgents/dev.agent-bridge.pump.plist (webhook retries every 60 s),
#           a one-time, idempotent import of the v1 JSON messages into ~/.agent-bridge/v2/bridge.db.
# The Executor route (gateway entry + Executor app) is separate: see DONE.txt.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
stamp=$(date +%Y%m%d%H%M%S)
bin=$HOME/bin/agent-bridge
ext=$HOME/.omp/agent/extensions/agent-bridge.ts
plist=$HOME/Library/LaunchAgents/dev.agent-bridge.pump.plist
label=dev.agent-bridge.pump
domain=gui/$(id -u)

aside() { # move a path out of the way, keeping it
	[ -e "$1" ] || [ -L "$1" ] || return 0
	mv "$1" "$1.bak-agent-bridge-v2-$stamp"
	echo "moved aside: $1 -> $1.bak-agent-bridge-v2-$stamp"
}

if [ "${1:-}" = rollback ]; then
	launchctl bootout "$domain/$label" 2>/dev/null || true
	aside "$plist"
	aside "$ext"
	if [ -f "$bin.v1" ]; then
		aside "$bin"
		mv "$bin.v1" "$bin"
		echo "restored v1 at $bin"
	fi
	echo "rolled back; the v2 store stays at ~/.agent-bridge/v2 (v1 never reads it). Restart omp sessions to drop the extension."
	exit 0
fi

# CLI: keep v1 as ~/bin/agent-bridge.v1 (once), then link v2
mkdir -p "$HOME/bin" "$HOME/.omp/agent/extensions" "$HOME/Library/LaunchAgents" "$HOME/.agent-bridge/logs"
if [ ! -L "$bin" ] || [ "$(readlink "$bin")" != "$repo/src/agent_bridge.py" ]; then
	if [ -f "$bin" ] && [ ! -L "$bin" ] && [ ! -e "$bin.v1" ] && grep -q '^VERSION = "0\.' "$bin"; then
		mv "$bin" "$bin.v1"
		echo "moved v1 aside: $bin.v1"
	else
		aside "$bin"
	fi
	ln -s "$repo/src/agent_bridge.py" "$bin"
fi
"$bin" migrate-v1 >/dev/null

# omp extension: every omp session started (or /reload-ed) from now on registers itself
if [ ! -L "$ext" ] || [ "$(readlink "$ext")" != "$repo/extension/agent-bridge.ts" ]; then
	aside "$ext"
	ln -s "$repo/extension/agent-bridge.ts" "$ext"
fi

# launchd: retry webhook sends every minute (recovers a crashed or killed detached sender)
rendered=$(sed "s|__HOME__|$HOME|g" "$repo/launchd/$label.plist")
if [ "$rendered" != "$(cat "$plist" 2>/dev/null)" ]; then
	launchctl bootout "$domain/$label" 2>/dev/null || true
	aside "$plist"
	printf '%s\n' "$rendered" > "$plist"
fi
launchctl print "$domain/$label" >/dev/null 2>&1 || launchctl bootstrap "$domain" "$plist"

echo "installed: $bin -> $(readlink "$bin")"
echo "           $ext -> $(readlink "$ext")"
echo "           $label: $(launchctl print "$domain/$label" | awk -F' = ' '/^\tstate/{print $2}')"
"$bin" status
