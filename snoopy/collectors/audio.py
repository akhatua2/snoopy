"""Audio collector — tracks microphone and speaker usage via CoreAudio.

Uses the CoreAudio C API (via ctypes) to check whether the default
input/output audio devices are running. When a device becomes active,
runs a targeted lsof to identify which process is using it.
Logs transitions only (active → inactive, inactive → active).
"""

import ctypes
import ctypes.util
import logging
import subprocess
import time
from ctypes import Structure, byref, c_uint32, sizeof

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

# ── CoreAudio constants ────────────────────────────────────────────────
# Four-char codes packed as uint32 (big-endian)
def _fourcc(s: str) -> int:
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])

kAudioObjectSystemObject = 1
kAudioHardwarePropertyDefaultInputDevice = _fourcc("dIn ")
kAudioHardwarePropertyDefaultOutputDevice = _fourcc("dOut")
kAudioDevicePropertyDeviceIsRunningSomewhere = _fourcc("gone")
kAudioObjectPropertyScopeGlobal = _fourcc("glob")
kAudioObjectPropertyElementMain = 0


class AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


# Load CoreAudio framework
_lib_path = ctypes.util.find_library("CoreAudio")
_ca = ctypes.cdll.LoadLibrary(_lib_path) if _lib_path else None


def _get_default_device(is_input: bool) -> int | None:
    """Get the AudioObjectID of the default input or output device."""
    if not _ca:
        return None
    if is_input:
        selector = kAudioHardwarePropertyDefaultInputDevice
    else:
        selector = kAudioHardwarePropertyDefaultOutputDevice
    addr = AudioObjectPropertyAddress(
        selector, kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    device_id = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    status = _ca.AudioObjectGetPropertyData(
        c_uint32(kAudioObjectSystemObject), byref(addr),
        c_uint32(0), None, byref(size), byref(device_id),
    )
    return device_id.value if status == 0 else None


def _is_device_running(device_id: int) -> bool:
    """Check if an audio device has any active audio streams."""
    if not _ca:
        return False
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyDeviceIsRunningSomewhere,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    running = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    status = _ca.AudioObjectGetPropertyData(
        c_uint32(device_id), byref(addr), c_uint32(0), None, byref(size), byref(running)
    )
    return running.value != 0 if status == 0 else False


def _find_audio_process() -> str:
    """Best-effort: find the process name currently using audio via lsof."""
    try:
        result = subprocess.run(
            ["lsof", "+c", "0", "-F", "cn", "+d", "/dev/"],
            capture_output=True, text=True, timeout=3,
        )
        # Look for processes with audio-related file descriptors
        for line in result.stdout.split("\n"):
            lower = line.lower()
            if "audio" in lower or "coreaudio" in lower:
                # Extract process name from lsof -F format
                if line.startswith("c"):
                    return line[1:]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: check common audio apps via the frontmost app
    return ""


AUDIO_INTERVAL = 3  # seconds


class AudioCollector(BaseCollector):
    name = "audio"
    interval = AUDIO_INTERVAL

    def setup(self) -> None:
        self._input_device = _get_default_device(is_input=True)
        self._output_device = _get_default_device(is_input=False)
        self._last_mic_active = False
        self._last_speaker_active = False
        self._last_mic_process = ""
        self._last_speaker_process = ""
        log.info(
            "audio devices: input=%s output=%s",
            self._input_device, self._output_device,
        )

    def collect(self) -> None:
        now = time.time()

        # Check mic
        if self._input_device:
            mic_active = _is_device_running(self._input_device)
            if mic_active != self._last_mic_active:
                process = _find_audio_process() if mic_active else self._last_mic_process
                self.buffer.push(Event(
                    table="audio_events",
                    columns=["timestamp", "device_type", "is_active", "process_name"],
                    values=(now, "microphone", int(mic_active), process),
                ))
                self._last_mic_active = mic_active
                self._last_mic_process = process

        # Check speaker
        if self._output_device:
            speaker_active = _is_device_running(self._output_device)
            if speaker_active != self._last_speaker_active:
                process = _find_audio_process() if speaker_active else self._last_speaker_process
                self.buffer.push(Event(
                    table="audio_events",
                    columns=["timestamp", "device_type", "is_active", "process_name"],
                    values=(now, "speaker", int(speaker_active), process),
                ))
                self._last_speaker_active = speaker_active
                self._last_speaker_process = process
