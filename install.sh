#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# snoopy installer — sets up the daemon as a macOS LaunchAgent
# ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.snoopy.daemon"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
DATA_DIR="$SCRIPT_DIR/data"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BOLD}[*]${NC} $1"; }
ok()    { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

echo ""
echo -e "${BOLD}  snoopy installer${NC}"
echo "  ──────────────────"
echo ""

# ── Preflight checks ──────────────────────────────────────────────────

info "Checking prerequisites..."

# Python venv
if [ ! -f "$PYTHON" ]; then
    fail "Virtual environment not found at .venv/\n    Run: uv sync"
fi
ok "Python venv found"

# uv / pip deps installed
if ! "$PYTHON" -c "import snoopy.daemon" 2>/dev/null; then
    fail "snoopy package not importable\n    Run: uv sync"
fi
ok "snoopy package importable"

# nowplaying-cli (optional but recommended)
if command -v nowplaying-cli &>/dev/null; then
    ok "nowplaying-cli found"
else
    warn "nowplaying-cli not found — media tracking will be limited"
    warn "  Install with: brew install nowplaying-cli"
fi

echo ""

# ── Shell history check ──────────────────────────────────────────────

if ! grep -q "EXTENDED_HISTORY" "$HOME/.zshrc" 2>/dev/null; then
    warn "EXTENDED_HISTORY not enabled in ~/.zshrc"
    echo "    Shell history collection needs these lines in ~/.zshrc:"
    echo ""
    echo "      setopt EXTENDED_HISTORY"
    echo "      setopt INC_APPEND_HISTORY"
    echo ""
fi

# ── Create data directory ─────────────────────────────────────────────

mkdir -p "$DATA_DIR"
ok "Data directory: $DATA_DIR"

# ── Generate LaunchAgent plist ────────────────────────────────────────

info "Generating LaunchAgent plist..."

cat > "$PLIST_SRC" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>snoopy.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityBackgroundIO</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DATA_DIR/snoopy.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$DATA_DIR/snoopy.stderr.log</string>
</dict>
</plist>
PLIST

ok "Plist generated at $PLIST_SRC"

# ── Install LaunchAgent ───────────────────────────────────────────────

# ── Trigger permission prompts ────────────────────────────────────────

info "Requesting macOS permissions (you may see system dialogs)..."

# Location Services — triggers the prompt by attempting to access location
"$PYTHON" -c "
import CoreLocation
mgr = CoreLocation.CLLocationManager.alloc().init()
mgr.requestAlwaysAuthorization()
print('  Location Services: prompt triggered')
" 2>/dev/null || warn "Could not trigger Location Services prompt"

# Accessibility — triggers the prompt by attempting to read window info
"$PYTHON" -c "
import Quartz
windows = Quartz.CGWindowListCopyWindowInfo(
    Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
)
print('  Accessibility: prompt triggered')
" 2>/dev/null || warn "Could not trigger Accessibility prompt"

# Full Disk Access — try to read a protected file (Safari history)
if [ -f "$HOME/Library/Safari/History.db" ]; then
    cat "$HOME/Library/Safari/History.db" > /dev/null 2>&1 || true
    ok "Full Disk Access: attempted read of protected file"
else
    warn "Full Disk Access: could not find Safari history to trigger prompt"
fi

echo ""
echo -e "  ${YELLOW}If you did not see permission prompts, open:${NC}"
echo "    System Settings > Privacy & Security"
echo "    and manually grant access to Terminal (or your terminal app)."
echo ""

# ── Register Claude Code hooks ────────────────────────────────────────

SNOOPY_HOOK="$SCRIPT_DIR/.venv/bin/snoopy-hook"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

if [ -f "$SNOOPY_HOOK" ]; then
    info "Registering Claude Code hooks..."

    "$PYTHON" -c "
import json, sys
from pathlib import Path

settings_path = Path('$CLAUDE_SETTINGS')
settings_path.parent.mkdir(parents=True, exist_ok=True)

if settings_path.exists():
    settings = json.loads(settings_path.read_text())
else:
    settings = {}

hooks = settings.setdefault('hooks', {})

snoopy_stop = {
    'matcher': '',
    'hooks': [{'type': 'command', 'command': '$SNOOPY_HOOK'}]
}
snoopy_start = {
    'matcher': '',
    'hooks': [{'type': 'command', 'command': '$SNOOPY_HOOK session-start'}]
}

# Add snoopy hooks without duplicating or removing existing ones
for event, hook_entry in [('Stop', snoopy_stop), ('SessionStart', snoopy_start), ('SessionEnd', snoopy_stop)]:
    entries = hooks.setdefault(event, [])
    # Remove any existing snoopy hooks
    entries[:] = [e for e in entries if 'snoopy-hook' not in json.dumps(e)]
    entries.append(hook_entry)

settings_path.write_text(json.dumps(settings, indent=2))
print('  Hooks registered in $CLAUDE_SETTINGS')
" 2>/dev/null && ok "Claude Code hooks registered" || warn "Could not register Claude Code hooks"
else
    warn "snoopy-hook not found at $SNOOPY_HOOK — Claude logging will use polling fallback"
fi

echo ""

# ── Install LaunchAgent ───────────────────────────────────────────────

info "Installing LaunchAgent..."

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"

ok "LaunchAgent loaded"

# ── Verify ────────────────────────────────────────────────────────────

sleep 1
if launchctl list | grep -q "$PLIST_NAME"; then
    ok "Daemon is running"
else
    warn "Daemon may not have started — check logs at $DATA_DIR/snoopy.stderr.log"
fi

# ── Summary ───────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}  Setup complete${NC}"
echo "  ──────────────────"
echo ""
echo "  Database     $DATA_DIR/snoopy.db"
echo "  Logs         $DATA_DIR/snoopy.log"
echo "  LaunchAgent  $PLIST_DST"
echo ""
echo "  Commands:"
echo "    Status     launchctl list | grep snoopy"
echo "    Stop       launchctl unload $PLIST_DST"
echo "    Restart    launchctl unload $PLIST_DST && launchctl load -w $PLIST_DST"
echo "    Uninstall  ./uninstall.sh"
echo ""
echo -e "  ${YELLOW}Permissions needed${NC} (System Settings > Privacy & Security):"
echo "    - Full Disk Access     (browser history, knowledgeC, notifications)"
echo "    - Location Services    (location tracking)"
echo "    - Accessibility        (window titles)"
echo ""
