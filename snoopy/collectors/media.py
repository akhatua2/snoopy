"""Media collector — tracks currently playing media via nowplaying-cli.

Deduplicates by only logging when the track (title+artist) changes.
Requires: brew install nowplaying-cli
"""

import logging
import subprocess
import time

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


class MediaCollector(BaseCollector):
    name = "media"
    interval = config.MEDIA_INTERVAL

    def setup(self) -> None:
        self._last_key: str | None = None

    def collect(self) -> None:
        info = self._get_now_playing()
        if info is None:
            return

        key = f"{info['title']}|{info['artist']}"
        if key == self._last_key:
            return
        self._last_key = key

        self.buffer.push(Event(
            table="media_events",
            columns=["timestamp", "title", "artist", "album", "app_source", "is_playing"],
            values=(
                time.time(),
                info["title"],
                info["artist"],
                info["album"],
                info["app"],
                int(info["playing"]),
            ),
        ))

    @staticmethod
    def _get_now_playing() -> dict | None:
        """Run nowplaying-cli and parse its output."""
        try:
            result = subprocess.run(
                [
                    "nowplaying-cli", "get", "title", "artist",
                    "album", "playbackRate", "clientPropertiesDeviceName",
                ],
                capture_output=True, text=True, timeout=3,
            )
        except FileNotFoundError:
            log.debug("nowplaying-cli not found")
            return None
        except subprocess.TimeoutExpired:
            return None

        if result.returncode != 0:
            return None

        lines = result.stdout.strip().split("\n")
        if len(lines) < 4:
            return None

        # null values show as "null"
        def clean(v: str) -> str:
            return "" if v.strip() == "null" else v.strip()

        title = clean(lines[0])
        if not title:
            return None

        return {
            "title": title,
            "artist": clean(lines[1]),
            "album": clean(lines[2]),
            "playing": lines[3].strip() != "0",
            "app": clean(lines[4]) if len(lines) > 4 else "",
        }
