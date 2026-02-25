"""Tests for audio collector — verifies mic/speaker transition detection."""

import time

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors.audio import AudioCollector, _is_device_running, _get_default_device


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


class TestCoreAudioAPI:
    def test_can_find_default_devices(self):
        """Verify CoreAudio API works and can find the default input/output devices.
        Should return integer device IDs (or None if no audio hardware)."""
        input_dev = _get_default_device(is_input=True)
        output_dev = _get_default_device(is_input=False)
        # On any Mac with audio hardware, these should be valid IDs
        assert input_dev is None or isinstance(input_dev, int)
        assert output_dev is None or isinstance(output_dev, int)

    def test_can_query_device_running_state(self):
        """Query whether the default output device is currently running.
        Should return a bool without crashing."""
        output_dev = _get_default_device(is_input=False)
        if output_dev:
            result = _is_device_running(output_dev)
            assert isinstance(result, bool)


class TestAudioCollector:
    def test_only_logs_on_state_transitions(self, buf, db, monkeypatch):
        """Simulate mic going active → still active → inactive.
        Should produce exactly 2 events (active transition + inactive transition),
        not 3 (the middle 'still active' poll should be skipped)."""

        # Mock CoreAudio to simulate: inactive → active → active → inactive
        states = iter([True, True, False])

        monkeypatch.setattr("snoopy.collectors.audio._is_device_running", lambda dev_id: next(states))
        monkeypatch.setattr("snoopy.collectors.audio._find_audio_process", lambda: "zoom.us")

        c = AudioCollector(buf, db)
        c._input_device = 42  # fake device ID
        c._output_device = None  # only test mic
        c._last_mic_active = False
        c._last_speaker_active = False
        c._last_mic_process = ""
        c._last_speaker_process = ""

        c.collect()  # inactive → active (logs)
        c.collect()  # active → active (skipped)
        c.collect()  # active → inactive (logs)
        buf.flush()

        assert db.count("audio_events") == 2

    def test_captures_process_name_on_activation(self, buf, db, monkeypatch):
        """When mic becomes active, the process using it (e.g. zoom.us) should
        be recorded in the event."""

        monkeypatch.setattr("snoopy.collectors.audio._is_device_running", lambda dev_id: True)
        monkeypatch.setattr("snoopy.collectors.audio._find_audio_process", lambda: "zoom.us")

        c = AudioCollector(buf, db)
        c._input_device = 42
        c._output_device = None
        c._last_mic_active = False
        c._last_speaker_active = False
        c._last_mic_process = ""
        c._last_speaker_process = ""

        c.collect()
        buf.flush()

        # Verify the event has the process name
        cur = db._ensure_conn().execute(
            "SELECT device_type, is_active, process_name FROM audio_events"
        )
        row = cur.fetchone()
        assert row == ("microphone", 1, "zoom.us")
