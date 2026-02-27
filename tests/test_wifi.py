"""Tests for wifi collector — verifies network change detection."""


import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.wifi import WifiCollector
from snoopy.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


class _FakeInterface:
    """Mock CWInterface that returns preset SSID/BSSID values."""
    def __init__(self, ssid, bssid):
        self._ssid = ssid
        self._bssid = bssid

    def ssid(self):
        return self._ssid

    def bssid(self):
        return self._bssid


class _FakeClient:
    """Mock CWWiFiClient that cycles through a list of networks."""
    def __init__(self, networks):
        self._networks = networks
        self._idx = 0

    def interface(self):
        ssid, bssid = self._networks[self._idx]
        self._idx += 1
        return _FakeInterface(ssid, bssid)


class TestWifiCollector:
    def test_only_logs_on_network_change(self, buf, db):
        """When connected to the same WiFi, only one event should be recorded.
        A second event should only appear when the SSID actually changes."""

        c = WifiCollector(buf, db)
        c._last_ssid = None
        c._client = _FakeClient([
            ("HomeWiFi", "aa:bb:cc"),
            ("HomeWiFi", "aa:bb:cc"),
            ("CoffeeShop", "dd:ee:ff"),
        ])

        c.collect()  # HomeWiFi — logs
        c.collect()  # HomeWiFi — skipped (same)
        c.collect()  # CoffeeShop — logs
        buf.flush()

        assert db.count("wifi_events") == 2
