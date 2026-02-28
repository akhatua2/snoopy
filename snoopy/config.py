"""Central configuration for snoopy daemon."""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env from project root
_env_path = _PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


def _default_data_dir() -> Path:
    env = os.environ.get("SNOOPY_DATA_DIR")
    if env:
        return Path(env)
    if (_PROJECT_ROOT / "pyproject.toml").exists():
        return _PROJECT_ROOT / "data"
    return Path.home() / ".snoopy"


DATA_DIR = _default_data_dir()
DB_PATH = DATA_DIR / "snoopy.db"
LOG_PATH = DATA_DIR / "snoopy.log"
PID_PATH = DATA_DIR / "snoopy.pid"

# ── Collection intervals (seconds) ────────────────────────────────────
WINDOW_INTERVAL = 2
BROWSER_INTERVAL = 30
SHELL_INTERVAL = 10
MEDIA_INTERVAL = 5
WIFI_INTERVAL = 30
CLIPBOARD_INTERVAL = 2
CLAUDE_INTERVAL = 15
NETWORK_INTERVAL = 60
FILESYSTEM_INTERVAL = 0  # FSEvents is push-based, 0 = no polling
LOCATION_INTERVAL = 300  # 5 minutes
NOTIFICATION_INTERVAL = 30
MESSAGES_INTERVAL = 15
BATTERY_INTERVAL = 300  # 5 minutes
SYSTEM_INTERVAL = 5     # sleep/wake detection + lock state polling
APPLIFECYCLE_INTERVAL = 10  # poll running apps for launches/quits
CALENDAR_INTERVAL = 1800    # 30 minutes
CALENDAR_HELPER = Path(__file__).resolve().parent.parent / "helpers" / "CalendarHelper.app"
OURA_INTERVAL = 86400       # once per day
OURA_PAT = os.environ.get("OURA_PAT", "")
MAIL_INTERVAL = 60          # poll every 60s
MAIL_SEED_DAYS = 1          # on first run, seed with last N days

# ── Buffer ─────────────────────────────────────────────────────────────
BUFFER_FLUSH_INTERVAL = 5  # seconds between flushes
BUFFER_MAX_SIZE = 500       # force flush if buffer exceeds this

# ── Browser history paths ──────────────────────────────────────────────
CHROME_HISTORY = Path("~/Library/Application Support/Google/Chrome/Default/History").expanduser()
ARC_HISTORY = Path("~/Library/Application Support/Arc/User Data/Default/History").expanduser()
SAFARI_HISTORY = Path("~/Library/Safari/History.db").expanduser()
FIREFOX_PROFILES = Path("~/Library/Application Support/Firefox/Profiles").expanduser()

# ── Shell history ──────────────────────────────────────────────────────
ZSH_HISTORY = Path(os.environ.get("HISTFILE", os.path.expanduser("~/.zsh_history")))

# ── Clipboard ──────────────────────────────────────────────────────────
CLIPBOARD_MAX_LENGTH = 10240  # 10 KB
CLIPBOARD_EXCLUDED_APPS = frozenset({
    "1Password",
    "1Password 7",
    "Bitwarden",
    "Keychain Access",
    "LastPass",
    "Dashlane",
})

# ── App lifecycle noise ───────────────────────────────────────────────
APP_EXCLUDED = frozenset({
    "Safari", "Music", "TV", "News", "Stocks", "Weather", "Phone", "Clock",
    "CoreServicesUIAgent", "CoreLocationAgent",
    "Cisco/Cisco Secure Client - Socket Filter",
    "Utilities/Nudge",
    "liquiddetectiond", "ManagedClient",
    "XProtect",
    "EscrowSecurityAlert", "TimeMachine/TMHelperAgent",
    "Setup Assistant", "Keychain Circle Notification",
})

# ── Filesystem noise ─────────────────────────────────────────────────
FS_EXCLUDED_PATTERNS = (
    "/snoopy/data/",
    "/.git/objects/",
    "/.git/refs/remotes/",
    "/.git/FETCH_HEAD",
    "/.git/ORIG_HEAD",
    "/.git/modules/",
    "/__pycache__/",
    "/.DS_Store",
    # Build artifacts
    "/target/",          # Rust
    "/node_modules/",
    "/.venv/", "/venv/", "/site-packages/",
    # Temp / download files
    ".crdownload", ".com.google.Chrome.", ".tmp", ".temp",
    # Log output
    "/logs/", ".traj.json",
)

# ── Claude logs ────────────────────────────────────────────────────────
CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()
CLAUDE_CONTENT_PREVIEW_LEN = 100_000

# ── Filesystem watcher ─────────────────────────────────────────────────
FS_WATCH_PATHS = [
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Desktop"),
]
FS_DEBOUNCE_SECONDS = 1.0

# ── Network ────────────────────────────────────────────────────────────
NETWORK_LSOF_TIMEOUT = 5  # seconds

# ── Daemon health ──────────────────────────────────────────────────────
HEALTH_HEARTBEAT_INTERVAL = 60
