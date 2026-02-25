"""Location collector — polls CLLocationManager for lat/lng."""

import time
import logging

import CoreLocation

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
from snoopy.config import LOCATION_INTERVAL

log = logging.getLogger(__name__)


class LocationCollector(BaseCollector):
    name = "location"
    interval = LOCATION_INTERVAL

    def setup(self) -> None:
        self._manager = CoreLocation.CLLocationManager.alloc().init()
        self._manager.requestAlwaysAuthorization()
        self._manager.setDesiredAccuracy_(CoreLocation.kCLLocationAccuracyHundredMeters)
        self._manager.startUpdatingLocation()
        log.info("CLLocationManager initialized")

    def teardown(self) -> None:
        self._manager.stopUpdatingLocation()

    def collect(self) -> None:
        location = self._manager.location()
        if location is None:
            log.debug("no location available yet")
            return

        coord = location.coordinate()
        self.buffer.push(Event(
            table="location_events",
            columns=["timestamp", "latitude", "longitude", "accuracy_m", "altitude_m", "source"],
            values=(
                time.time(),
                coord.latitude,
                coord.longitude,
                location.horizontalAccuracy(),
                location.altitude(),
                "corelocation",
            ),
        ))
