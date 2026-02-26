"""Window collector — tracks active app, window title, input state, and display.

Logs one row per app/window switch. Each row includes:
- Which app and window title had focus
- How long you stayed there
- Whether you were typing or mousing when you left (keyboard_idle_s, mouse_idle_s)
- Which display/monitor the window was on
"""

import subprocess
import time
import logging

import Quartz
from AppKit import NSWorkspace, NSScreen

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)

_CHROMIUM_BUNDLE_IDS = {
    "com.google.Chrome",
    "company.thebrowser.Browser",  # Arc
}

_CHROMIUM_TAB_SCRIPTS = {
    "com.google.Chrome": 'tell application "Google Chrome" to get title of active tab of front window',
    "company.thebrowser.Browser": 'tell application "Arc" to get title of active tab of front window',
}


def _get_chromium_tab_title(bundle_id: str) -> str:
    """Get active tab title from a Chromium browser via AppleScript."""
    script = _CHROMIUM_TAB_SCRIPTS.get(bundle_id)
    if not script:
        return ""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""

# CGEventSource event type constants
_kCGEventKeyDown = 10
_kCGEventMouseMoved = 5


def _get_keyboard_idle() -> float:
    """Seconds since last keyboard event."""
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState,
        _kCGEventKeyDown,
    )


def _get_mouse_idle() -> float:
    """Seconds since last mouse movement."""
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState,
        _kCGEventMouseMoved,
    )


def _get_display_for_window(bounds: dict) -> str:
    """Determine which display a window is on based on its position."""
    if not bounds:
        return ""
    win_x = bounds.get("X", 0)
    win_y = bounds.get("Y", 0)
    win_cx = win_x + bounds.get("Width", 0) / 2
    win_cy = win_y + bounds.get("Height", 0) / 2

    for screen in NSScreen.screens():
        frame = screen.frame()
        sx, sy = frame.origin.x, frame.origin.y
        sw, sh = frame.size.width, frame.size.height
        if sx <= win_cx <= sx + sw and sy <= win_cy <= sy + sh:
            name = screen.localizedName()
            return str(name) if name else "unknown"
    return ""


class WindowCollector(BaseCollector):
    name = "window"
    interval = config.WINDOW_INTERVAL

    def setup(self) -> None:
        self._last_app: str | None = None
        self._last_title: str | None = None
        self._last_bundle: str | None = None
        self._last_display: str = ""
        self._last_ts: float = 0.0

    def collect(self) -> None:
        workspace = NSWorkspace.sharedWorkspace()
        active = workspace.activeApplication()
        if not active:
            return

        app_name = active.get("NSApplicationName", "")
        bundle_id = active.get("NSApplicationBundleIdentifier", "")
        title, bounds = self._get_frontmost_window_info()

        if not title and bundle_id in _CHROMIUM_BUNDLE_IDS:
            title = _get_chromium_tab_title(bundle_id)

        # Deduplicate: skip if same app + title
        if app_name == self._last_app and title == self._last_title:
            return

        # Title flicker: if same app but new title is empty, keep the real title
        if app_name == self._last_app and not title and self._last_title:
            return

        now = time.time()
        duration = now - self._last_ts if self._last_ts else 0.0

        # Snapshot input state at the moment of switch
        kb_idle = _get_keyboard_idle()
        mouse_idle = _get_mouse_idle()
        display = _get_display_for_window(bounds)

        # Emit the *previous* window's event with duration and input state at departure
        if self._last_app and self._last_ts:
            self.buffer.push(Event(
                table="window_events",
                columns=[
                    "timestamp", "app_name", "window_title", "bundle_id",
                    "duration_s", "keyboard_idle_s", "mouse_idle_s", "display_id",
                ],
                values=(
                    self._last_ts, self._last_app, self._last_title, self._last_bundle,
                    duration, kb_idle, mouse_idle, self._last_display,
                ),
            ))

        self._last_app = app_name
        self._last_title = title
        self._last_bundle = bundle_id
        self._last_display = display
        self._last_ts = now

    @staticmethod
    def _get_frontmost_window_info() -> tuple[str, dict]:
        """Get the title and bounds of the frontmost window."""
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        if not windows:
            return ("", {})
        for win in windows:
            if win.get(Quartz.kCGWindowLayer, -1) == 0:
                title = win.get(Quartz.kCGWindowName, "") or ""
                bounds = win.get(Quartz.kCGWindowBounds, {})
                return (title, dict(bounds) if bounds else {})
        return ("", {})
