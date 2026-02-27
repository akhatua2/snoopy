"""Filesystem collector — watches directories for changes via FSEvents.

Uses macOS FSEvents (push-based, not polling). Watches ~/Documents,
~/Downloads, ~/Desktop and any configured project directories.
Debounces rapid events on the same path.
"""

import logging
import threading
import time

import FSEvents

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


class FilesystemCollector(BaseCollector):
    name = "filesystem"
    interval = 0  # Push-based via FSEvents callback

    def setup(self) -> None:
        self._last_events: dict[str, float] = {}  # path -> timestamp for debounce
        self._stream = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.setup()
        watch_paths = [p for p in config.FS_WATCH_PATHS if p]
        if not watch_paths:
            log.warning("no filesystem watch paths configured")
            return

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run_fsevents,
            args=(watch_paths,),
            name="collector-filesystem",
            daemon=True,
        )
        self._thread.start()
        log.info("[%s] watching %d paths", self.name, len(watch_paths))

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream:
            FSEvents.FSEventStreamStop(self._stream)
            FSEvents.FSEventStreamInvalidate(self._stream)
            FSEvents.FSEventStreamRelease(self._stream)
            self._stream = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        log.info("[%s] stopped", self.name)

    def collect(self) -> None:
        pass  # Push-based; events come from callback

    def _run_fsevents(self, watch_paths: list[str]) -> None:
        """Run the FSEvents stream on a CFRunLoop."""
        from Foundation import NSDate, NSRunLoop

        def callback(stream_ref, client_info, num_events, event_paths, event_flags, event_ids):
            now = time.time()
            for i in range(num_events):
                path = event_paths[i]
                # Debounce: skip if we saw this path recently
                last_seen = self._last_events.get(path, 0)
                if now - last_seen < config.FS_DEBOUNCE_SECONDS:
                    continue
                self._last_events[path] = now

                path_str = (
                    path.decode("utf-8", errors="replace")
                    if isinstance(path, bytes) else str(path)
                )

                if any(pat in path_str for pat in config.FS_EXCLUDED_PATTERNS):
                    continue

                flags = event_flags[i]
                event_type = self._classify_flags(flags)

                self.buffer.push(Event(
                    table="file_events",
                    columns=["timestamp", "event_type", "file_path", "directory"],
                    values=(
                        now, event_type, path_str,
                        path_str.rsplit("/", 1)[0] if "/" in path_str else "",
                    ),
                ))

        self._stream = FSEvents.FSEventStreamCreate(
            None,               # allocator
            callback,           # callback
            None,               # context
            watch_paths,        # paths to watch
            FSEvents.kFSEventStreamEventIdSinceNow,
            1.0,                # latency (seconds)
            FSEvents.kFSEventStreamCreateFlagFileEvents | FSEvents.kFSEventStreamCreateFlagNoDefer,
        )

        FSEvents.FSEventStreamScheduleWithRunLoop(
            self._stream,
            FSEvents.CFRunLoopGetCurrent(),
            FSEvents.kCFRunLoopDefaultMode,
        )
        FSEvents.FSEventStreamStart(self._stream)

        # Run the loop until stop is signaled
        while not self._stop_event.is_set():
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.5)
            )

    @staticmethod
    def _classify_flags(flags: int) -> str:
        """Map FSEvent flags to a human-readable event type."""
        if flags & FSEvents.kFSEventStreamEventFlagItemCreated:
            return "created"
        if flags & FSEvents.kFSEventStreamEventFlagItemRemoved:
            return "removed"
        if flags & FSEvents.kFSEventStreamEventFlagItemRenamed:
            return "renamed"
        if flags & FSEvents.kFSEventStreamEventFlagItemModified:
            return "modified"
        return "unknown"
