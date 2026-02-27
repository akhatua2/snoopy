"""Event cleaning & timeline builder.

Queries raw events from SQLite, deduplicates, normalizes, filters noise,
and produces a unified Action timeline for SFT dataset construction.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import snoopy.config as config

# ── Data structures ──────────────────────────────────────────────────────


@dataclass(slots=True)
class Action:
    timestamp: float
    action_type: str  # e.g. "focus", "browse", "cmd", "claude:Write"
    text: str  # e.g. "Chrome: GitHub Pull Requests"

    def format(self, show_time: bool = False) -> str:
        if show_time:
            from datetime import datetime

            t = datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
            return f"[{t}] [{self.action_type}] {self.text}"
        return f"[{self.action_type}] {self.text}"


SESSION_BREAK = "SESSION_BREAK"

# ── Noise filters ────────────────────────────────────────────────────────

_SYSTEM_NOISE_APPS = frozenset(
    {
        "loginwindow",
        "ScreenSaverEngine",
        "CoreServicesUIAgent",
        "universalAccessAuthWarn",
        "UserNotificationCenter",
        "SystemUIServer",
        "Dock",
        "Finder",
        "Window Server",
        "WindowServer",
    }
)

_BRAILLE_RE = re.compile(r"[⠀-⣿✳⠂⠐⠒]+")
_SCREENSHOT_RE = re.compile(r"Screenshot \d{4}-\d{2}-\d{2} at \d+\.\d+\.\d+ [AP]M\.png")
_GENERATED_IMAGE_RE = re.compile(r"Generated Image \w+ \d+, \d{4} - \d+_\d+[AP]M\.\w+\.\w+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "twclid",
}
_BROWSER_SUFFIXES = re.compile(r"\s*[-–—]\s*(Google Chrome|Mozilla Firefox|Arc|Safari|Brave)$")
_NOTIF_COUNT_RE = re.compile(r"^\(\d+\)\s+")
_TERMINAL_PROMPT_RE = re.compile(
    r"^\([\w-]+\)\s*\([\w-]+\)\s*\S+@\S+\s+\S+\s+%\s*"  # (env) (base) user@host dir %
    r"|^\([\w-]+\)\s*\S+@\S+\s+\S+\s+%\s*"  # (env) user@host dir %
    r"|^\S+@\S+\s+\S+\s+%\s*"  # user@host dir %
)
_SKIP_URLS = re.compile(r"^(chrome-extension://|about:|chrome://|arc://)")

# Spam/redirect domains to filter from browse events
_SPAM_DOMAINS = frozenset(
    {
        "netoda.tech",
        "fmovies.to",
        "fmovies.ps",
        "fmovies.llc",
        "fmoviesz.to",
        "fmovies2.to",
        "fmovies.co",
    }
)

_FS_SKIP_PATTERNS = (
    # Version control & build artifacts
    "/.git/",
    "/node_modules/",
    "/__pycache__/",
    ".pyc",
    ".DS_Store",
    "/target/",  # Rust build output
    "/.venv/",
    "/venv/",
    "/site-packages/",
    ".egg-info/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/.mypy_cache/",
    # Snoopy's own data
    "/snoopy/data/",
    # Log & trajectory files (background process output)
    "/logs/",
    ".traj.json",
    ".log",
    # Lock files & metadata
    "/uv.lock",
    "/package-lock.json",
    "/yarn.lock",
    # Temp files
    "/tmp/",
    ".tmp",
    ".temp",
    # Binary/media (not user-editable text)
    ".rmeta",
    ".rcgu.o",
    ".d",
    ".dylib",
    ".so",
    # Chrome/browser temp files
    ".com.google.Chrome.",
    ".crdownload",
    # Screenshot temp files
    ".Screenshot ",
)
_FS_PROJECT_PREFIXES = (
    str(Path.home() / "Documents"),
    str(Path.home() / "Downloads"),
    str(Path.home() / "Desktop"),
)

_SKIP_CLAUDE_TYPES = {"assistant_text", "tool_result"}

_SENSITIVE_CLIP_RE = re.compile(
    r"token=|api_key=|secret=|password=|login/one-time"
    r"|^ghp_|^gho_|^github_pat_|^sk-[a-zA-Z0-9]{20}|^xox[bpas]-|^AKIA[0-9A-Z]{16}",
    re.IGNORECASE | re.MULTILINE,
)


def _build_contact_map() -> dict[str, str]:
    """Try to resolve phone numbers → contact names via macOS Contacts framework.

    Returns empty dict if Contacts access is unavailable.
    """
    try:
        import objc

        objc.loadBundle(
            "Contacts",
            {},
            bundle_path="/System/Library/Frameworks/Contacts.framework",
        )
        CNContactStore = objc.lookUpClass("CNContactStore")
        CNContact = objc.lookUpClass("CNContact")

        store = CNContactStore.alloc().init()
        keys = ["givenName", "familyName", "phoneNumbers"]
        container_id = store.defaultContainerIdentifier()
        if not container_id:
            return {}
        pred = CNContact.predicateForContactsInContainerWithIdentifier_(container_id)
        contacts = store.unifiedContactsMatchingPredicate_keysToFetch_error_(pred, keys, None)
        if not contacts or not isinstance(contacts, (list, tuple)):
            return {}

        mapping: dict[str, str] = {}
        for contact in contacts:
            given = contact.givenName() or ""
            family = contact.familyName() or ""
            name = f"{given} {family}".strip()
            if not name:
                continue
            for labeled in contact.phoneNumbers():
                number = labeled.value().stringValue()
                digits = "".join(c for c in number if c.isdigit() or c == "+")
                if digits:
                    mapping[digits] = name
        return mapping
    except Exception:
        return {}


_CONTACT_MAP: dict[str, str] | None = None


def _resolve_contact(phone: str) -> str:
    """Resolve a phone number to a contact name if possible."""
    global _CONTACT_MAP
    if _CONTACT_MAP is None:
        _CONTACT_MAP = _build_contact_map()
    if not _CONTACT_MAP:
        return phone
    # Try exact match, then without country code
    name = _CONTACT_MAP.get(phone)
    if not name and phone.startswith("+1"):
        name = _CONTACT_MAP.get(phone[2:])
    if not name and phone.startswith("+"):
        name = _CONTACT_MAP.get(phone[1:])
    return name or phone


# ── Path normalization ───────────────────────────────────────────────────

_HOME = str(Path.home())
_DESKTOP = str(Path.home() / "Desktop")


def _normalize_path(p: str) -> str:
    if not p:
        return p
    if p.startswith("/tmp/") or (p.startswith(_HOME) and "/Library/" in p):
        return ""
    if p.startswith(_DESKTOP + "/"):
        rest = p[len(_DESKTOP) + 1 :]
        return rest
    if p.startswith(_HOME):
        return "~" + p[len(_HOME) :]
    return p


def _clean_url(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in params.items() if k not in _TRACKING_PARAMS}
    new_query = urlencode(cleaned, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _clean_title(title: str, max_len: int = 100) -> str:
    if not title:
        return ""
    title = _BRAILLE_RE.sub("", title).strip()
    # Strip browser audio indicator (🔊) — same tab shows different titles
    # when audio plays/stops, creating false switches
    if "🔊" in title:
        title = title.replace("🔊", "").strip()
    # Normalize generated filenames with timestamps
    title = _SCREENSHOT_RE.sub("[screenshot]", title)
    title = _GENERATED_IMAGE_RE.sub("[generated-image]", title)
    # Redact email addresses (PII)
    title = _EMAIL_RE.sub("[email]", title)
    if len(title) > max_len:
        title = title[:max_len] + "..."
    return title


def _clean_command(cmd: str) -> str:
    if not cmd:
        return ""
    cmd = _ANSI_RE.sub("", cmd)
    cmd = cmd.replace(_HOME, "~")
    if len(cmd) > 150:
        cmd = cmd[:150] + "..."
    return cmd.strip()


# ── Per-table cleaners ───────────────────────────────────────────────────


def _clean_window_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, app_name, window_title, duration_s "
        "FROM window_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, app, title, dur in rows:
        if not app:
            continue
        if app in _SYSTEM_NOISE_APPS or app in config.APP_EXCLUDED:
            continue
        if dur is not None and dur < 1.0:
            continue

        title = _clean_title(title or "")

        if app in ("Code", "Code - Insiders") and title:
            parts = title.split(" — ", 1)
            if parts:
                title = parts[0].strip()

        if app in ("iTerm2", "Terminal") and title:
            # Extract the part after ":" (user@host: path → path)
            if ":" in title:
                title = title.split(":", 1)[1].strip()
            # Only keep titles that look like directory paths — strip
            # session names, leaked titles, and other noise
            if not (title.startswith("~") or title.startswith("/")):
                title = ""

        text = f"{app}: {title}" if title else app
        actions.append(Action(ts, "focus", text))

    return actions


def _clean_browser_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, url, title FROM browser_events "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, url, title in rows:
        if not url or _SKIP_URLS.match(url):
            continue
        # Skip spam/redirect domains (check base domain)
        parsed_url = urlparse(url)
        netloc = parsed_url.netloc.lower()
        if any(netloc == d or netloc.endswith("." + d) for d in _SPAM_DOMAINS):
            continue
        url = _clean_url(url)
        title = title or ""
        title = _BROWSER_SUFFIXES.sub("", title)
        title = _NOTIF_COUNT_RE.sub("", title)
        title = _clean_title(title)
        if not title:
            title = parsed_url.netloc or url[:60]
        actions.append(Action(ts, "browse", title))

    return actions


def _clean_shell_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, command FROM shell_events "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, cmd in rows:
        cmd = _clean_command(cmd)
        if not cmd or cmd == "cd":
            continue
        actions.append(Action(ts, "cmd", cmd))

    return actions


def _clean_claude_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, message_type, content_preview "
        "FROM claude_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, msg_type, preview in rows:
        if not msg_type:
            continue
        if msg_type in _SKIP_CLAUDE_TYPES:
            continue

        preview = (preview or "").strip()

        if msg_type == "user":
            text = preview[:80] if preview else ""
            if not text:
                continue  # skip empty user turns (tool_result acks)
            # Skip system-generated messages
            if text.lstrip().startswith("<"):
                continue
            if text.startswith("This session is being continued"):
                continue
            # Strip terminal prompt prefix if user pasted terminal output
            text = _TERMINAL_PROMPT_RE.sub("", text)
            # Normalize paths in user messages
            text = text.replace(_HOME + "/", "~/")
            if not text.strip():
                continue
            actions.append(Action(ts, "claude:user", text))
        elif msg_type.startswith("tool_use:"):
            tool = msg_type.split(":", 1)[1]
            if tool in ("Write", "Edit", "Read"):
                path = preview.split("\n", 1)[0].strip() if preview else ""
                path = _normalize_path(path)
                if path:
                    actions.append(Action(ts, f"claude:{tool}", path))
            elif tool == "Bash":
                text = preview[:80] if preview else ""
                actions.append(Action(ts, "claude:Bash", text))
            elif tool == "Grep":
                text = preview[:80] if preview else ""
                actions.append(Action(ts, "claude:Grep", text))
            elif tool == "Glob":
                text = preview[:80] if preview else ""
                actions.append(Action(ts, "claude:Glob", text))

    return actions


def _clean_message_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, contact, is_from_me, content_preview, service, chat_name "
        "FROM message_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, contact, is_from_me, preview, service, chat_name in rows:
        direction = "message:sent" if is_from_me else "message:recv"
        contact = _resolve_contact(contact) if contact else "Unknown"
        service = service or "iMessage"
        preview = (preview or "")[:50]
        text = f"{contact} via {service}"
        if chat_name and chat_name != contact:
            text += f" ({chat_name})"
        if preview:
            text += f': "{preview}"'
        actions.append(Action(ts, direction, text))

    return actions


def _clean_notification_events(
    conn: sqlite3.Connection, since: float, until: float
) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, app_name, content_preview "
        "FROM notification_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, app, preview in rows:
        if not app:
            continue
        if ts < 820454400:
            continue
        preview = (preview or "")[:60]
        text = f"{app}: {preview}" if preview else app
        actions.append(Action(ts, "notify", text))

    return actions


def _clean_clipboard_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, content_text, source_app "
        "FROM clipboard_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, content, source in rows:
        if not content:
            continue
        if len(content) > 500:
            continue
        if source and source in config.CLIPBOARD_EXCLUDED_APPS:
            continue
        # Skip clipboard content with auth tokens or secrets
        if _SENSITIVE_CLIP_RE.search(content):
            continue
        text = content[:60].replace("\n", " ").strip()
        if not text:
            continue
        # Strip terminal prompt prefix from copied terminal output
        text = _TERMINAL_PROMPT_RE.sub("", text).strip()
        if not text:
            continue
        # Normalize paths
        text = text.replace(_HOME + "/", "~/")
        actions.append(Action(ts, "clipboard", text))

    return actions


def _clean_app_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, event_type, app_name "
        "FROM app_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, etype, app in rows:
        if not app:
            continue
        if app in _SYSTEM_NOISE_APPS or app in config.APP_EXCLUDED:
            continue
        if any(
            x in app.lower()
            for x in (
                "helper",
                "agent",
                "daemon",
                "extension",
                "screencaptureui",
                "textinputswitcher",
                "inputmethod",
            )
        ):
            continue
        action_type = "launch" if etype == "launch" else "quit"
        actions.append(Action(ts, action_type, app))

    return actions


def _clean_system_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, event_type FROM system_events "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, etype in rows:
        if etype in ("sleep", "wake", "lock", "unlock"):
            actions.append(Action(ts, etype, ""))

    return actions


def _clean_file_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, event_type, file_path "
        "FROM file_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, etype, fpath in rows:
        if not fpath:
            continue
        if not any(fpath.startswith(p) for p in _FS_PROJECT_PREFIXES):
            continue
        if any(pat in fpath for pat in _FS_SKIP_PATTERNS):
            continue
        type_map = {"modified": "edit", "created": "create", "removed": "delete"}
        action_type = type_map.get(etype)
        if not action_type:
            continue
        path = _normalize_path(fpath)
        if not path:
            continue
        # Skip directory creates (no file extension) — only keep actual files
        basename = path.rsplit("/", 1)[-1] if "/" in path else path
        if action_type == "create" and "." not in basename:
            continue
        # Normalize generated filenames with timestamps
        if "Screenshot" in basename and basename.endswith(".png"):
            path_dir = path.rsplit("/", 1)[0] if "/" in path else ""
            path = f"{path_dir}/[screenshot]" if path_dir else "[screenshot]"
        elif "Generated Image" in basename:
            path_dir = path.rsplit("/", 1)[0] if "/" in path else ""
            path = f"{path_dir}/[generated-image]" if path_dir else "[generated-image]"
        actions.append(Action(ts, action_type, path))

    return actions


def _clean_mail_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, sender, subject, is_from_me "
        "FROM mail_events WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, sender, subject, is_from_me in rows:
        direction = "mail:sent" if is_from_me else "mail:recv"
        subject = (subject or "")[:60]
        if is_from_me:
            # Sender is always the user for sent mail — just show subject
            text = f'"{subject}"' if subject else "(no subject)"
        else:
            sender = sender or "Unknown"
            text = f'{sender}: "{subject}"' if subject else sender
        actions.append(Action(ts, direction, text))

    return actions


def _clean_audio_events(conn: sqlite3.Connection, since: float, until: float) -> list[Action]:
    rows = conn.execute(
        "SELECT timestamp, device_type, is_active "
        "FROM audio_events WHERE timestamp >= ? AND timestamp < ? "
        "AND device_type = 'input' ORDER BY timestamp",
        (since, until),
    ).fetchall()

    actions = []
    for ts, _, is_active in rows:
        action_type = "mic:on" if is_active else "mic:off"
        actions.append(Action(ts, action_type, ""))

    return actions


# ── Deduplication ────────────────────────────────────────────────────────


def _dedup_focus(actions: list[Action]) -> list[Action]:
    """Collapse consecutive same-app focus events within 5s. Keep the LAST one."""
    if not actions:
        return actions

    result: list[Action] = []
    i = 0
    while i < len(actions):
        a = actions[i]
        if a.action_type != "focus":
            result.append(a)
            i += 1
            continue

        app = a.text.split(":", 1)[0].strip()
        best = a
        j = i + 1
        while j < len(actions):
            b = actions[j]
            if b.action_type != "focus":
                break
            b_app = b.text.split(":", 1)[0].strip()
            if b_app != app or (b.timestamp - best.timestamp) > 5.0:
                break
            best = b
            j += 1
        result.append(best)
        i = j

    return result


def _dedup_browse(actions: list[Action]) -> list[Action]:
    """Collapse consecutive same-title browse events within 30s."""
    if not actions:
        return actions

    result: list[Action] = []
    for a in actions:
        if (
            a.action_type == "browse"
            and result
            and result[-1].action_type == "browse"
            and result[-1].text == a.text
            and (a.timestamp - result[-1].timestamp) < 30.0
        ):
            continue
        result.append(a)
    return result


def _dedup_commands(actions: list[Action]) -> list[Action]:
    """Collapse consecutive identical shell commands."""
    if not actions:
        return actions

    result: list[Action] = []
    for a in actions:
        if (
            a.action_type == "cmd"
            and result
            and result[-1].action_type == "cmd"
            and result[-1].text == a.text
        ):
            continue
        result.append(a)
    return result


def _dedup_file_events(actions: list[Action]) -> list[Action]:
    """Collapse rapid file events (edit/create) to same file within 10s."""
    if not actions:
        return actions

    _FILE_TYPES = {"edit", "create"}
    result: list[Action] = []
    for a in actions:
        if (
            a.action_type in _FILE_TYPES
            and result
            and result[-1].action_type in _FILE_TYPES
            and result[-1].text == a.text
            and (a.timestamp - result[-1].timestamp) < 10.0
        ):
            continue
        result.append(a)
    return result


def _dedup_clipboard(actions: list[Action]) -> list[Action]:
    """Collapse consecutive identical clipboard copies."""
    if not actions:
        return actions

    result: list[Action] = []
    for a in actions:
        if (
            a.action_type == "clipboard"
            and result
            and result[-1].action_type == "clipboard"
            and result[-1].text == a.text
        ):
            continue
        result.append(a)
    return result


def _dedup_mail(actions: list[Action]) -> list[Action]:
    """Collapse duplicate mail events (same text, any time gap)."""
    if not actions:
        return actions

    seen_mail: set[str] = set()
    result: list[Action] = []
    for a in actions:
        if a.action_type in ("mail:recv", "mail:sent"):
            key = f"{a.action_type}:{a.text}"
            if key in seen_mail:
                continue
            seen_mail.add(key)
        result.append(a)
    return result


_BROWSER_APPS = frozenset(
    {
        "Google Chrome",
        "Chrome",
        "Arc",
        "Safari",
        "Firefox",
        "Brave Browser",
        "Microsoft Edge",
        "Orion",
    }
)


def _is_browser_focus(action: Action) -> bool:
    """Check if a focus action is for a browser app."""
    app = action.text.split(":", 1)[0].strip()
    return app in _BROWSER_APPS


# Patterns that clearly belong to a browser/web page, not a desktop app
_LEAKED_TITLE_RE = re.compile(
    r"🔊|FMovies|Instagram|"
    r" \| Modal$| \| OpenReview$| – Weights & Biases$| – Google Cloud|"
    r" · "  # web page separator (e.g. "Cursor · CLI")
)
_MESSAGES_BAD_TITLE_RE = re.compile(
    r"\.(py|md|json|sh|log|svg|png|jpg|js|ts|css|html|yaml|yml|toml)\b|"
    r" — |"  # VS Code "file — project" separator
    r"⁨|⁩|"  # macOS invisible unicode markers in Finder titles
    r" \| |"  # web page title separator
    r"@\w+\.\w+|"  # email addresses (Gmail leaks)
    r"Weights & Biases|Workspace|Modal$|🔊"
)


def _fix_leaked_titles(actions: list[Action]) -> list[Action]:
    """Fix window titles leaked from the previously-focused app.

    macOS's accessibility API sometimes reports a stale window title from the
    app you just switched away from.  Detect this by checking if a non-browser
    app's title exactly matches a different app's title seen in the last 15 s.
    Also applies app-specific heuristics for Messages, Calendar, etc.
    """
    if not actions:
        return actions

    # Track most-recently-seen title per app
    recent: dict[str, tuple[str, float]] = {}  # app -> (title, ts)
    result: list[Action] = []

    for a in actions:
        if a.action_type != "focus":
            result.append(a)
            continue

        app = a.text.split(":", 1)[0].strip()
        title = a.text.split(":", 1)[1].strip() if ":" in a.text else ""

        strip = False

        if title:
            # 1) Generic: title matches a DIFFERENT app's recent title
            for other_app, (other_title, other_ts) in recent.items():
                if other_app != app and other_title == title and (a.timestamp - other_ts) < 15.0:
                    strip = True
                    break

            # 2) Non-browser app with obviously-browser title
            if not strip and app not in _BROWSER_APPS:
                if _LEAKED_TITLE_RE.search(title):
                    strip = True

            # 3) Messages should have contact/chat names, not file paths
            if not strip and app == "Messages":
                if _MESSAGES_BAD_TITLE_RE.search(title):
                    strip = True

        if strip:
            result.append(Action(a.timestamp, "focus", app))
        else:
            result.append(a)

        # Update tracking
        if title:
            recent[app] = (title, a.timestamp)

    return result


def _cross_table_dedup(actions: list[Action]) -> list[Action]:
    """Drop [focus] BrowserApp: X when [browse] X exists within 5s."""
    if len(actions) < 2:
        return actions

    # Build index of browse titles → timestamps
    browse_times: dict[str, list[float]] = {}
    for a in actions:
        if a.action_type == "browse":
            browse_times.setdefault(a.text, []).append(a.timestamp)

    to_drop: set[int] = set()
    for i, a in enumerate(actions):
        if a.action_type != "focus" or not _is_browser_focus(a):
            continue
        title = a.text.split(":", 1)[1].strip() if ":" in a.text else ""
        if not title:
            continue
        for bts in browse_times.get(title, []):
            if abs(bts - a.timestamp) < 5.0:
                to_drop.add(i)
                break

    return [a for i, a in enumerate(actions) if i not in to_drop]


# ── Session segmentation ────────────────────────────────────────────────


def _insert_session_breaks(actions: list[Action]) -> list[Action]:
    """Insert SESSION_BREAK markers at sleep/wake, long locks, and gaps >30min."""
    if not actions:
        return actions

    result: list[Action] = []
    for i, a in enumerate(actions):
        if a.action_type in ("sleep", "wake"):
            result.append(Action(a.timestamp, SESSION_BREAK, ""))
            continue

        if i > 0 and result:
            gap = a.timestamp - actions[i - 1].timestamp
            if gap > 600 and actions[i - 1].action_type == "lock":
                result.append(Action(a.timestamp, SESSION_BREAK, ""))
            elif gap > 1800:
                result.append(Action(a.timestamp, SESSION_BREAK, ""))

        result.append(a)

    return result


# ── Main entry point ────────────────────────────────────────────────────


def build_timeline(
    db_path: str,
    since_ts: float = 0,
    until_ts: float | None = None,
) -> list[Action]:
    """Build a cleaned, deduplicated action timeline from the snoopy database.

    Args:
        db_path: Path to snoopy.db
        since_ts: Start timestamp (inclusive). 0 = beginning of time.
        until_ts: End timestamp (exclusive). None = now.

    Returns:
        Sorted list of Action, with SESSION_BREAK markers.
    """
    if until_ts is None:
        import time

        until_ts = time.time()

    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")

    try:
        all_actions: list[Action] = []
        all_actions.extend(_clean_window_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_browser_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_shell_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_claude_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_message_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_notification_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_clipboard_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_app_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_system_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_file_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_mail_events(conn, since_ts, until_ts))
        all_actions.extend(_clean_audio_events(conn, since_ts, until_ts))

        all_actions.sort(key=lambda a: a.timestamp)

        all_actions = _fix_leaked_titles(all_actions)
        all_actions = _dedup_focus(all_actions)
        all_actions = _dedup_browse(all_actions)
        all_actions = _dedup_commands(all_actions)
        all_actions = _dedup_file_events(all_actions)
        all_actions = _dedup_clipboard(all_actions)
        all_actions = _dedup_mail(all_actions)
        all_actions = _cross_table_dedup(all_actions)
        all_actions = _insert_session_breaks(all_actions)

        return all_actions
    finally:
        conn.close()
