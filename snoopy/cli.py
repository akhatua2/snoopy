"""snoopy CLI — install, start, stop, and manage the snoopy daemon."""

import argparse
import os
import shutil
import subprocess
import sqlite3
import sys
import textwrap
import time
from pathlib import Path

from snoopy.config import DATA_DIR, DB_PATH, LOG_PATH, PID_PATH

_PLIST_LABEL = "com.snoopy.daemon"
_PLIST_DST = Path.home() / "Library/LaunchAgents" / f"{_PLIST_LABEL}.plist"


def _python() -> str:
    return sys.executable


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _is_running() -> bool:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True
        ).stdout
        return _PLIST_LABEL in out
    except OSError:
        return False


def _pid() -> int | None:
    if PID_PATH.exists():
        try:
            return int(PID_PATH.read_text().strip())
        except ValueError:
            pass
    return None


def _generate_plist() -> str:
    python = _python()
    root = _project_root()
    data = DATA_DIR
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_PLIST_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python}</string>
                <string>-m</string>
                <string>snoopy.daemon</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
                <key>SNOOPY_DATA_DIR</key>
                <string>{data}</string>
            </dict>
            <key>WorkingDirectory</key>
            <string>{root}</string>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>ProcessType</key>
            <string>Background</string>
            <key>LowPriorityBackgroundIO</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{data}/snoopy.stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{data}/snoopy.stderr.log</string>
        </dict>
        </plist>""")


# ── Permissions ───────────────────────────────────────────────────────────


def _trigger_permissions() -> None:
    """Trigger macOS permission prompts so users see system dialogs during install."""
    print("  Requesting macOS permissions (you may see system dialogs)...\n")

    # Accessibility — reading window info requires this
    try:
        import Quartz
        Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        print("  [+] Accessibility: prompt triggered")
    except Exception:
        print("  [!] Accessibility: could not trigger — grant manually in System Settings")

    # Location Services — run CoreLocationCLI once to trigger its prompt
    cli = shutil.which("CoreLocationCLI")
    if cli:
        try:
            subprocess.run([cli, "-once"], capture_output=True, timeout=10)
            print("  [+] Location Services: prompt triggered")
        except (subprocess.TimeoutExpired, OSError):
            print("  [!] Location Services: could not trigger — grant manually in System Settings")

    # Full Disk Access — attempt to read a protected file
    safari_hist = Path.home() / "Library/Safari/History.db"
    if safari_hist.exists():
        try:
            safari_hist.read_bytes()[:1]
            print("  [+] Full Disk Access: already granted")
        except PermissionError:
            print("  [!] Full Disk Access: not yet granted")
    else:
        print("  [!] Full Disk Access: could not probe (no Safari history file)")

    print()
    print("  If you did not see permission prompts, open:")
    print("    System Settings > Privacy & Security")
    print("    and grant access to your terminal app.\n")


def _check_shell_history() -> None:
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists() and "EXTENDED_HISTORY" in zshrc.read_text():
        print("  [+] EXTENDED_HISTORY enabled in ~/.zshrc")
    else:
        print("  [!] EXTENDED_HISTORY not enabled in ~/.zshrc")
        print("      Shell history collection needs these lines in ~/.zshrc:")
        print("        setopt EXTENDED_HISTORY")
        print("        setopt INC_APPEND_HISTORY")


def _check_optional_deps() -> None:
    if shutil.which("CoreLocationCLI"):
        print("  [+] CoreLocationCLI found")
    else:
        print("  [!] CoreLocationCLI not found — install with: brew install corelocationcli")

    if shutil.which("nowplaying-cli"):
        print("  [+] nowplaying-cli found")
    else:
        print("  [!] nowplaying-cli not found — media tracking will be limited")
        print("      Install with: brew install nowplaying-cli")


# ── Subcommands ──────────────────────────────────────────────────────────


def cmd_install(args: argparse.Namespace) -> None:
    print("\n  snoopy install")
    print("  ──────────────────\n")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  [+] Data directory: {DATA_DIR}")

    _check_optional_deps()
    _check_shell_history()
    print()

    _trigger_permissions()

    plist_content = _generate_plist()
    _PLIST_DST.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_DST.write_text(plist_content)
    print(f"  [+] LaunchAgent written to {_PLIST_DST}")

    snoopy_hook = Path(_python()).parent / "snoopy-hook"
    claude_settings = Path.home() / ".claude" / "settings.json"
    if snoopy_hook.exists():
        _register_claude_hooks(snoopy_hook, claude_settings)

    subprocess.run(["launchctl", "unload", str(_PLIST_DST)],
                   capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(_PLIST_DST)],
                   capture_output=True)
    print("  [+] LaunchAgent loaded")

    time.sleep(1)
    if _is_running():
        print("  [+] Daemon is running")
    else:
        print("  [!] Daemon may not have started — check: snoopy logs")

    print(f"""
  Install complete
  ──────────────────

  Database     {DB_PATH}
  Logs         {LOG_PATH}
  LaunchAgent  {_PLIST_DST}

  Commands:
    snoopy status      check daemon status
    snoopy stop        stop the daemon
    snoopy start       start the daemon
    snoopy logs        view recent logs
    snoopy uninstall   remove everything
""")


def _register_claude_hooks(hook_path: Path, settings_path: Path) -> None:
    import json
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            pass

    hooks = settings.setdefault("hooks", {})
    hook_cmd = str(hook_path)
    stop_entry = {"matcher": "", "hooks": [{"type": "command", "command": hook_cmd}]}
    start_entry = {"matcher": "", "hooks": [{"type": "command", "command": f"{hook_cmd} session-start"}]}

    for event, entry in [("Stop", stop_entry), ("SessionStart", start_entry), ("SessionEnd", stop_entry)]:
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries if "snoopy-hook" not in json.dumps(e)]
        entries.append(entry)

    settings_path.write_text(json.dumps(settings, indent=2))
    print("  [+] Claude Code hooks registered")


def cmd_uninstall(args: argparse.Namespace) -> None:
    print("\n  snoopy uninstall")
    print("  ──────────────────\n")

    if _PLIST_DST.exists():
        subprocess.run(["launchctl", "unload", str(_PLIST_DST)], capture_output=True)
        _PLIST_DST.unlink()
        print("  [+] LaunchAgent removed")
    else:
        print("  [ ] LaunchAgent not found (already removed?)")

    subprocess.run(["pkill", "-f", "snoopy.daemon"], capture_output=True)

    if args.remove_data:
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR)
            print(f"  [+] Data directory removed: {DATA_DIR}")
    else:
        print(f"  [ ] Data directory kept at: {DATA_DIR}")
        print("      To remove: snoopy uninstall --remove-data")

    print("\n  snoopy uninstalled\n")


def cmd_start(args: argparse.Namespace) -> None:
    if _is_running():
        print("snoopy daemon is already running")
        return

    if not _PLIST_DST.exists():
        print("LaunchAgent not found. Run: snoopy install")
        return

    subprocess.run(["launchctl", "load", "-w", str(_PLIST_DST)], capture_output=True)
    time.sleep(1)
    if _is_running():
        print("snoopy daemon started")
    else:
        print("failed to start — check: snoopy logs")


def cmd_stop(args: argparse.Namespace) -> None:
    if not _is_running():
        print("snoopy daemon is not running")
        return

    subprocess.run(["launchctl", "unload", str(_PLIST_DST)], capture_output=True)
    print("snoopy daemon stopped")


def cmd_restart(args: argparse.Namespace) -> None:
    if _is_running():
        subprocess.run(["launchctl", "unload", str(_PLIST_DST)], capture_output=True)
        print("snoopy daemon stopped")

    time.sleep(1)

    if not _PLIST_DST.exists():
        print("LaunchAgent not found. Run: snoopy install")
        return

    subprocess.run(["launchctl", "load", "-w", str(_PLIST_DST)], capture_output=True)
    time.sleep(1)
    if _is_running():
        print("snoopy daemon started")
    else:
        print("failed to start — check: snoopy logs")


def cmd_status(args: argparse.Namespace) -> None:
    running = _is_running()
    pid = _pid()

    print(f"\n  snoopy status")
    print(f"  ──────────────────\n")
    print(f"  Daemon       {'running' if running else 'stopped'}" +
          (f" (pid {pid})" if running and pid else ""))
    print(f"  Data dir     {DATA_DIR}")
    print(f"  LaunchAgent  {'installed' if _PLIST_DST.exists() else 'not installed'}")

    if DB_PATH.exists():
        size_mb = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"  Database     {size_mb:.1f} MB")

        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall() if not r[0].startswith("sqlite_")]
            total = 0
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM [{t}]")
                count = cur.fetchone()[0]
                total += count
            conn.close()
            print(f"  Events       {total:,} across {len(tables)} tables")
        except sqlite3.Error:
            pass
    else:
        print("  Database     not created yet")

    print()


def cmd_logs(args: argparse.Namespace) -> None:
    log_file = LOG_PATH
    if not log_file.exists():
        stderr_log = DATA_DIR / "snoopy.stderr.log"
        if stderr_log.exists():
            log_file = stderr_log
        else:
            print(f"No log files found at {DATA_DIR}")
            return

    lines = log_file.read_text().splitlines()
    n = args.lines
    for line in lines[-n:]:
        print(line)


def cmd_menubar(args: argparse.Namespace) -> None:
    from snoopy.menubar import main as menubar_main
    menubar_main()


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snoopy",
        description="macOS activity collection daemon",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="install and start the snoopy daemon")

    p_uninstall = sub.add_parser("uninstall", help="stop and remove snoopy")
    p_uninstall.add_argument("--remove-data", action="store_true",
                             help="also delete collected data")

    sub.add_parser("start", help="start the daemon")
    sub.add_parser("stop", help="stop the daemon")
    sub.add_parser("restart", help="restart the daemon")
    sub.add_parser("status", help="show daemon status and stats")
    sub.add_parser("menubar", help="launch the macOS menu bar app")

    p_logs = sub.add_parser("logs", help="show recent log output")
    p_logs.add_argument("-n", "--lines", type=int, default=30,
                        help="number of lines to show (default: 30)")

    args = parser.parse_args()

    commands = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
        "menubar": cmd_menubar,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
