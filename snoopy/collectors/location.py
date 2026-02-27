"""Location collector — uses CoreLocationCLI to get lat/lng + address.

CoreLocationCLI is a proper .app bundle so macOS grants it location
permission via the standard system dialog.  We shell out to it rather
than using CLLocationManager directly, which requires an NSRunLoop and
an app bundle with NSLocationUsageDescription.
"""

import logging
import shutil
import subprocess
import time

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
from snoopy.config import LOCATION_INTERVAL

log = logging.getLogger(__name__)

_CLI = shutil.which("CoreLocationCLI")
_SEP = "||"
_FORMAT = _SEP.join([
    "%latitude", "%longitude", "%altitude", "%h_accuracy",
    "%address", "%locality", "%administrativeArea", "%country",
])


class LocationCollector(BaseCollector):
    name = "location"
    interval = LOCATION_INTERVAL

    def setup(self) -> None:
        if not _CLI:
            log.warning("CoreLocationCLI not found — install with: "
                        "brew install corelocationcli")

    def collect(self) -> None:
        if not _CLI:
            return

        try:
            result = subprocess.run(
                [_CLI, "-once", "--format", _FORMAT],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.debug("CoreLocationCLI failed: %s", e)
            return

        if result.returncode != 0 or not result.stdout.strip():
            return

        line = result.stdout.strip().replace("\n", ", ")
        parts = line.split(_SEP)
        if len(parts) < 8:
            return

        try:
            lat, lng = float(parts[0]), float(parts[1])
            alt, acc = float(parts[2]), float(parts[3])
        except ValueError:
            return

        address = parts[4].strip() or None
        locality = parts[5].strip() or None
        admin_area = parts[6].strip() or None
        country = parts[7].strip() or None

        self.buffer.push(Event(
            table="location_events",
            columns=["timestamp", "latitude", "longitude", "accuracy_m",
                     "altitude_m", "address", "locality", "admin_area",
                     "country", "source"],
            values=(time.time(), lat, lng, acc, alt,
                    address, locality, admin_area, country,
                    "corelocationcli"),
        ))
