#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.snoopy.daemon"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
DATA_DIR="$SCRIPT_DIR/data"

echo "=== snoopy uninstaller ==="

# 1. Unload LaunchAgent
if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "LaunchAgent removed"
else
    echo "LaunchAgent not found (already removed?)"
fi

# 2. Kill any running daemon
pkill -f "snoopy.daemon" 2>/dev/null || true

# 3. Optionally remove data
echo ""
read -p "Remove data directory ($DATA_DIR)? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$DATA_DIR"
    echo "Data directory removed"
else
    echo "Data directory kept at: $DATA_DIR"
fi

echo ""
echo "=== snoopy uninstalled ==="
