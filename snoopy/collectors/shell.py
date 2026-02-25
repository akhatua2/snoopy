"""Shell history collector — reads ~/.zsh_history incrementally.

Expects EXTENDED_HISTORY format: `: timestamp:elapsed;command`
Tracks file byte offset as watermark so we only read new entries.
"""

import logging
import os
import re
import time

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)

_EXTENDED_RE = re.compile(r"^: (\d+):(\d+);(.*)$")


class ShellCollector(BaseCollector):
    name = "shell"
    interval = config.SHELL_INTERVAL

    def setup(self) -> None:
        saved = self.get_watermark()
        if saved is not None:
            self._offset = int(saved)
        elif config.ZSH_HISTORY.exists():
            # First run: skip to end of file so we only track new commands
            self._offset = config.ZSH_HISTORY.stat().st_size
            log.info("[%s] first run — skipping existing history, tracking new commands only", self.name)
        else:
            self._offset = 0

    def collect(self) -> None:
        if not config.ZSH_HISTORY.exists():
            return

        file_size = config.ZSH_HISTORY.stat().st_size
        if file_size <= self._offset:
            # File was truncated or unchanged
            if file_size < self._offset:
                self._offset = 0
            return

        events = []
        with open(config.ZSH_HISTORY, "r", errors="replace") as f:
            f.seek(self._offset)
            for line in f:
                line = line.rstrip("\n")
                m = _EXTENDED_RE.match(line)
                if m:
                    ts = float(m.group(1))
                    elapsed = float(m.group(2))
                    cmd = m.group(3)
                    events.append(Event(
                        table="shell_events",
                        columns=["timestamp", "command", "elapsed_seconds"],
                        values=(ts, cmd, elapsed),
                    ))
            self._offset = f.tell()

        if events:
            self.buffer.push_many(events)
            self.set_watermark(str(self._offset))
            log.info("[%s] collected %d commands", self.name, len(events))
