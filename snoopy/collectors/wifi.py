"""WiFi collector — tracks current SSID via CoreWLAN framework.

Only logs when the network changes (deduplication).
Requires Location Services permission to read SSID on modern macOS.
"""

import logging
import time

import objc

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)

objc.loadBundle("CoreWLAN", globals(), bundle_path="/System/Library/Frameworks/CoreWLAN.framework")


class WifiCollector(BaseCollector):
    name = "wifi"
    interval = config.WIFI_INTERVAL

    def setup(self) -> None:
        self._last_ssid: str | None = None
        self._client = CWWiFiClient.sharedWiFiClient()  # noqa: F821

    def collect(self) -> None:
        iface = self._client.interface()
        if iface is None:
            return

        ssid = iface.ssid() or ""
        bssid = iface.bssid() or ""

        if ssid == self._last_ssid:
            return
        self._last_ssid = ssid

        self.buffer.push(Event(
            table="wifi_events",
            columns=["timestamp", "ssid", "bssid"],
            values=(time.time(), ssid, bssid),
        ))
