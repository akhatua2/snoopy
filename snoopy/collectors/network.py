"""Network collector — tracks established TCP connections via lsof.

Runs `lsof -i -P -n` and filters for ESTABLISHED connections.
Deduplicates: only logs NEW connections that weren't seen in the previous poll.
"""

import logging
import subprocess
import time

import snoopy.config as config
from snoopy._native import parse_lsof_output
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


class NetworkCollector(BaseCollector):
    name = "network"
    interval = config.NETWORK_INTERVAL

    def setup(self) -> None:
        self._seen: set[tuple[str, str, int]] = set()

    def collect(self) -> None:
        try:
            result = subprocess.run(
                ["lsof", "-i", "-P", "-n"],
                capture_output=True, text=True,
                timeout=config.NETWORK_LSOF_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.warning("lsof failed or timed out")
            return

        if result.returncode != 0:
            return

        now = time.time()
        current: set[tuple[str, str, int]] = parse_lsof_output(result.stdout)

        # Only log connections we haven't seen before
        new_connections = current - self._seen
        events = []
        for process_name, remote_addr, remote_port in new_connections:
            events.append(Event(
                table="network_events",
                columns=["timestamp", "process_name", "protocol", "remote_address", "remote_port"],
                values=(now, process_name, "TCP", remote_addr, remote_port),
            ))

        self._seen = current

        if events:
            self.buffer.push_many(events)
            log.info(
                "[%s] %d new connections (%d total active)",
                self.name, len(events), len(current),
            )
