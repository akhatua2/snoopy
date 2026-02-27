"""snoopy menu bar — floating panel with animated pixel-art sprite."""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import AppKit
import objc
from Foundation import NSNotificationCenter, NSObject, NSTimer
from PyObjCTools import AppHelper

_PLIST_LABEL = "com.snoopy.daemon"
_PLIST_DST = Path.home() / "Library/LaunchAgents" / f"{_PLIST_LABEL}.plist"
_W, _H = 220, 210
_RADIUS = 14.0
_PX = 4  # each logical pixel = 4x4 points in the panel

# Sprite colors
_BLACK = AppKit.NSColor.blackColor()
_CREAM = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.97, 0.98, 0.91, 1.0)
_COLORS = {1: _BLACK, 2: _CREAM}


def _rainbow_colors(tick, n_rows):
    """Build per-row rainbow color list for the cream fill."""
    colors = []
    for row in range(n_rows):
        hue = (tick * 0.08 + row * 0.08) % 1.0
        colors.append(AppKit.NSColor.colorWithHue_saturation_brightness_alpha_(hue, 0.7, 1.0, 1.0))
    return colors


# ── Sprite data (22x12, 0=transparent 1=black 2=cream) ──────────────────

# fmt: off
IDLE = [
    [0,0,0,0,1,1,0,0,0,0,0,0,0,0,1,0,0,0,0,1,0,0],
    [0,0,0,1,2,2,1,0,0,0,0,0,0,1,2,1,0,0,1,2,1,0],
    [0,0,0,1,2,2,1,0,0,0,0,0,0,1,2,1,0,0,1,2,1,0],
    [0,1,1,2,1,1,0,0,0,0,0,0,0,1,2,2,1,1,2,2,1,0],
    [1,2,2,1,0,0,0,0,1,1,1,1,0,1,2,2,2,2,2,2,1,0],
    [1,2,2,1,0,0,0,1,2,2,2,2,1,1,2,1,2,2,1,2,1,0],
    [1,2,2,1,0,0,1,2,2,2,2,2,1,2,2,2,1,1,2,2,2,1],
    [1,2,2,2,1,1,1,2,2,2,1,2,2,1,1,1,2,2,1,1,1,0],
    [0,1,1,1,2,2,1,2,2,2,2,1,2,2,2,2,1,1,2,1,0,0],
    [0,0,0,0,1,1,1,2,2,2,2,2,1,2,2,2,2,2,1,2,1,0],
    [0,0,0,0,1,1,1,2,2,2,2,2,1,2,2,2,2,2,1,2,1,0],
    [0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,0],
]
# fmt: on

# Tail positions (rows 0-2, cols 0-6)
# fmt: off
_TAIL = {
    'right': [
        [0,0,0,0,1,1,0],
        [0,0,0,1,2,2,1],
        [0,0,0,1,2,2,1],
    ],
    'mid': [
        [0,0,0,1,1,0,0],
        [0,0,1,2,2,1,0],
        [0,0,1,2,2,1,0],
    ],
    'left': [
        [0,0,1,1,0,0,0],
        [0,1,2,2,1,0,0],
        [0,1,2,2,1,0,0],
    ],
}
# fmt: on

_TAIL_CYCLE = ["right", "mid", "left", "mid"]  # 4-tick period


def _compose_frame(tail="right", look="center", eyes="open", ear="both"):
    """Build a sprite frame from independent animation layers."""
    grid = [row[:] for row in IDLE]

    # Tail position (rows 0-2, cols 0-6)
    t = _TAIL[tail]
    for r in range(3):
        grid[r][:7] = t[r]

    # Eye positions depend on look direction
    if look == "left":
        eye_cols = (14, 17)
        grid[5][15] = 2
        grid[5][18] = 2
        grid[5][14] = 1
        grid[5][17] = 1
    elif look == "right":
        eye_cols = (16, 19)
        grid[5][15] = 2
        grid[5][18] = 2
        grid[5][16] = 1
        grid[5][19] = 1
    else:
        eye_cols = (15, 18)

    # Blink: set current eye positions to cream
    if eyes == "closed":
        grid[5][eye_cols[0]] = 2
        grid[5][eye_cols[1]] = 2

    # Ear twitch (row 0)
    if ear == "left_down":
        grid[0][14] = 0
    elif ear == "right_down":
        grid[0][19] = 0

    return grid


# ── Sprite rendering ─────────────────────────────────────────────────────


class _SpriteView(AppKit.NSView):
    """Draws a pixel grid with crisp nearest-neighbor style."""

    _grid = None
    _row_colors = None
    _px = _PX

    def isFlipped(self):
        return True

    def setGrid_(self, grid):
        self._grid = grid
        self.setNeedsDisplay_(True)

    def setRowColors_(self, row_colors):
        self._row_colors = row_colors

    def drawRect_(self, dirty):
        if not self._grid:
            return
        ctx = AppKit.NSGraphicsContext.currentContext()
        ctx.setShouldAntialias_(False)
        ctx.setImageInterpolation_(AppKit.NSImageInterpolationNone)
        px = self._px
        for y, row in enumerate(self._grid):
            for x, cell in enumerate(row):
                if cell == 2 and self._row_colors:
                    c = self._row_colors[y]
                else:
                    c = _COLORS.get(cell)
                if c:
                    c.set()
                    AppKit.NSRectFill(((x * px, y * px), (px, px)))


def _grid_to_image(grid, px=3, row_colors=None):
    """Render a pixel grid to an NSImage (bottom-up coords)."""
    rows, cols = len(grid), len(grid[0])
    w, h = cols * px, rows * px
    img = AppKit.NSImage.alloc().initWithSize_((w, h))
    img.lockFocus()
    ctx = AppKit.NSGraphicsContext.currentContext()
    ctx.setShouldAntialias_(False)
    ctx.setImageInterpolation_(AppKit.NSImageInterpolationNone)
    for gy, row in enumerate(grid):
        fy = (rows - 1 - gy) * px
        for gx, cell in enumerate(row):
            if cell == 2 and row_colors:
                c = row_colors[gy]
            else:
                c = _COLORS.get(cell)
            if c:
                c.set()
                AppKit.NSRectFill(((gx * px, fy), (px, px)))
    img.unlockFocus()
    return img


# ── Helpers ──────────────────────────────────────────────────────────────


def _is_running():
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return _PLIST_LABEL in out
    except OSError:
        return False


def _btn(title, x, y, w, h=30, color=None):
    b = AppKit.NSButton.alloc().initWithFrame_(((x, y), (w, h)))
    b.setTitle_(title)
    b.setBezelStyle_(1)
    b.setFont_(AppKit.NSFont.systemFontOfSize_weight_(12, 0.3))
    if color:
        b.setBezelColor_(color)
        b.setContentTintColor_(AppKit.NSColor.whiteColor())
    return b


# ── Panel & background ───────────────────────────────────────────────────


class _Panel(AppKit.NSPanel):
    def initWithRect_(self, rect):
        mask = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        self = objc.super(_Panel, self).initWithContentRect_styleMask_backing_defer_(
            rect, mask, AppKit.NSBackingStoreBuffered, False
        )
        if self is None:
            return None
        self.setLevel_(AppKit.NSStatusWindowLevel)
        self.setHasShadow_(True)
        self.setOpaque_(False)
        self.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorTransient
        )
        return self

    def canBecomeKeyWindow(self):
        return True


class _RoundedVisualView(AppKit.NSVisualEffectView):
    def initWithFrame_(self, frame):
        self = objc.super(_RoundedVisualView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setMaterial_(AppKit.NSVisualEffectMaterialPopover)
        self.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        self.setState_(AppKit.NSVisualEffectStateActive)
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(_RADIUS)
        self.layer().setMasksToBounds_(True)
        return self


# ── Main controller ──────────────────────────────────────────────────────


class StatusBarController(NSObject):
    def init(self):
        self = objc.super(StatusBarController, self).init()
        if self is None:
            return None

        # Status bar item
        self._item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        btn = self._item.button()
        btn.setImage_(self._make_icon(_compose_frame()))
        btn.setTarget_(self)
        btn.setAction_("toggle:")

        # Floating panel
        self._panel = _Panel.alloc().initWithRect_(((0, 0), (_W, _H)))
        bg = _RoundedVisualView.alloc().initWithFrame_(((0, 0), (_W, _H)))
        self._panel.setContentView_(bg)
        self._content = bg

        self._build_ui()
        self._tick = 0
        self._running = _is_running()
        self._training_active = False

        # Dismiss on focus loss
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "panelLostFocus:", AppKit.NSWindowDidResignKeyNotification, self._panel
        )

        # Animation timer (0.3s per tick)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "animate:", None, True
        )

        # Daemon status check (every 5s)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "checkDaemon:", None, True
        )

        self._update_buttons()
        self._animate()

        # Start training schedule (12h interval) — delayed to avoid import fork
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, "startTrainingSchedule:", None, False
        )

        return self

    def _build_ui(self):
        v = self._content
        y = _H

        # Sprite (22*4=88 x 12*4=48)
        y -= 18
        sw = len(IDLE[0]) * _PX
        sh = len(IDLE) * _PX
        self._sprite = _SpriteView.alloc().initWithFrame_((((_W - sw) / 2, y - sh), (sw, sh)))
        v.addSubview_(self._sprite)
        y -= sh

        # Buttons (centered)
        y -= 16
        bw = 82
        gap = 8
        x0 = (_W - (bw * 2 + gap)) / 2

        self._toggle_btn = _btn("Start", x0, y - 30, bw, color=AppKit.NSColor.systemGreenColor())
        self._toggle_btn.setTarget_(self)
        self._toggle_btn.setAction_("onToggleDaemon:")
        v.addSubview_(self._toggle_btn)

        q = _btn("Quit", x0 + bw + gap, y - 30, bw)
        q.setTarget_(self)
        q.setAction_("onQuit:")
        v.addSubview_(q)
        y -= 30

        # ── Training section ────────────────────────────────────────
        y -= 12

        # Separator line
        sep = AppKit.NSBox.alloc().initWithFrame_(((16, y - 1), (_W - 32, 1)))
        sep.setBoxType_(AppKit.NSBoxSeparator)
        v.addSubview_(sep)
        y -= 5

        # "Training" label
        y -= 14
        lbl = AppKit.NSTextField.labelWithString_("Training")
        lbl.setFrame_((((_W - 80) / 2, y), (80, 14)))
        lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
        lbl.setFont_(AppKit.NSFont.systemFontOfSize_weight_(10, 0.5))
        lbl.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        v.addSubview_(lbl)

        # Status text (monospace, small)
        y -= 18
        self._train_status = AppKit.NSTextField.labelWithString_("--")
        self._train_status.setFrame_(((10, y), (_W - 20, 14)))
        self._train_status.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._train_status.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(9, 0.0))
        self._train_status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        v.addSubview_(self._train_status)

        # "Train Now" button
        y -= 34
        tw = 100
        self._train_btn = _btn(
            "Train Now", (_W - tw) / 2, y, tw, color=AppKit.NSColor.systemBlueColor()
        )
        self._train_btn.setTarget_(self)
        self._train_btn.setAction_("onTrainNow:")
        v.addSubview_(self._train_btn)

    @staticmethod
    def _make_icon(grid, row_colors=None):
        img = _grid_to_image(grid, px=3, row_colors=row_colors)
        img.setSize_((33, 18))
        return img

    @objc.typedSelector(b"v@:@")
    def startTrainingSchedule_(self, _timer):
        threading.Thread(target=self._start_training_schedule_bg, daemon=True).start()

    @staticmethod
    def _start_training_schedule_bg():
        from linus.sync import start_schedule

        start_schedule()

    def _animate(self):
        self._tick += 1
        t = self._tick

        if not self._running:
            # Sleeping: eyes closed, still, occasional ear twitch (dreaming)
            ear_t = t % 30
            ear = "left_down" if ear_t == 10 else "right_down" if ear_t == 20 else "both"
            grid = _compose_frame(eyes="closed", ear=ear)
        else:
            tail = _TAIL_CYCLE[t % 4]
            look_t = t % 20
            look = "right" if look_t in (8, 9) else "left" if look_t in (16, 17) else "center"
            eyes = "closed" if t % 12 == 0 else "open"
            ear_t = t % 30
            ear = "left_down" if ear_t == 10 else "right_down" if ear_t == 20 else "both"
            grid = _compose_frame(tail=tail, look=look, eyes=eyes, ear=ear)

        row_colors = _rainbow_colors(self._tick, 12) if self._training_active else None
        self._item.button().setImage_(self._make_icon(grid, row_colors))

        if self._panel.isVisible():
            self._sprite.setRowColors_(row_colors)
            self._sprite.setGrid_(grid)

    def _update_buttons(self):
        if self._running:
            self._toggle_btn.setTitle_("Stop")
            self._toggle_btn.setBezelColor_(AppKit.NSColor.systemOrangeColor())
        else:
            self._toggle_btn.setTitle_("Start")
            self._toggle_btn.setBezelColor_(AppKit.NSColor.systemGreenColor())

    def _show(self):
        self._running = _is_running()
        self._update_buttons()
        self._update_training_ui()
        self._animate()
        btn_rect = self._item.button().window().frame()
        mid_x = btn_rect.origin.x + btn_rect.size.width / 2
        x = mid_x - _W / 2
        y = btn_rect.origin.y - _H - 4
        self._panel.setFrame_display_(((x, y), (_W, _H)), True)
        self._panel.makeKeyAndOrderFront_(None)

    def _hide(self):
        self._panel.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def toggle_(self, _sender):
        if self._panel.isVisible():
            self._hide()
        else:
            self._show()

    @objc.typedSelector(b"v@:@")
    def panelLostFocus_(self, _note):
        self._hide()

    @objc.typedSelector(b"v@:@")
    def animate_(self, _timer):
        self._animate()

    @objc.typedSelector(b"v@:@")
    def checkDaemon_(self, _timer):
        self._running = _is_running()
        if self._panel.isVisible():
            self._update_buttons()
            self._update_training_ui()

    @objc.typedSelector(b"v@:@")
    def onToggleDaemon_(self, _sender):
        self._running = not self._running
        self._update_buttons()
        self._animate()
        threading.Thread(target=self._toggle_daemon_bg, daemon=True).start()

    def _toggle_daemon_bg(self):
        if self._running:
            if _PLIST_DST.exists():
                subprocess.run(["launchctl", "load", "-w", str(_PLIST_DST)], capture_output=True)
        else:
            subprocess.run(["launchctl", "unload", str(_PLIST_DST)], capture_output=True)

    @objc.typedSelector(b"v@:@")
    def onTrainNow_(self, _sender):
        from linus.sync import trigger_train

        started = trigger_train()
        if started:
            self._train_btn.setEnabled_(False)
            self._train_btn.setTitle_("Training...")
            self._train_status.setStringValue_("Starting...")
        # If already running, button is already disabled

    def _update_training_ui(self):
        from linus.sync import STATE_PATH, is_training

        training = is_training()
        self._training_active = training
        self._train_btn.setEnabled_(not training)
        if training:
            self._train_btn.setTitle_("Training...")
        else:
            self._train_btn.setTitle_("Train Now")

        if not STATE_PATH.exists():
            self._train_status.setStringValue_("No training yet")
            return

        try:
            state = __import__("json").loads(STATE_PATH.read_text())
        except (ValueError, OSError):
            return

        status = state.get("status", "idle")
        if status != "idle":
            self._train_status.setStringValue_(status.replace("_", " ").title() + "...")
            return

        parts = []
        v = state.get("adapter_version", 0)
        if v:
            parts.append(f"v{v}")
        eval_metrics = state.get("last_eval_metrics")
        if eval_metrics and "score" in eval_metrics:
            parts.append(f"Score: {eval_metrics['score']:.0%}")
        ts = state.get("last_train_complete_ts")
        if ts:
            parts.append(self._format_ago(ts))

        err = state.get("last_error")
        if parts:
            self._train_status.setStringValue_(" | ".join(parts))
        elif err == "eval_below_threshold":
            self._train_status.setStringValue_("Score too low, retrying...")
        elif err:
            self._train_status.setStringValue_(err.replace("_", " ").title())
        else:
            self._train_status.setStringValue_("No training yet")

    @staticmethod
    def _format_ago(ts):
        delta = time.time() - ts
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"

    @objc.typedSelector(b"v@:@")
    def onQuit_(self, _sender):
        from linus.sync import stop_schedule

        stop_schedule()
        AppKit.NSApplication.sharedApplication().terminate_(None)


_keep_alive = []


_PIDFILE = Path(__file__).resolve().parent.parent / "data" / "menubar.pid"


def main():
    # Single-instance guard
    _PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    if _PIDFILE.exists():
        old_pid = int(_PIDFILE.read_text().strip())
        try:
            os.kill(old_pid, 0)  # check if alive
            os.kill(old_pid, 9)  # kill it
            time.sleep(0.5)
        except (ProcessLookupError, ValueError):
            pass
    _PIDFILE.write_text(str(os.getpid()))

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    ctrl = StatusBarController.alloc().init()
    _keep_alive.append(ctrl)
    signal.signal(signal.SIGINT, lambda *_: AppHelper.stopEventLoop())
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
