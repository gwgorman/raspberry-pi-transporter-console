import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flight_telemetry import FlightTelemetryService, PIAWARE_BASES, _optional_json, _tile_xy


class FlightTelemetryTests(unittest.TestCase):
    def test_receiver_location_maps_to_expected_nexrad_tile(self):
        self.assertEqual(_tile_xy(33.142790, -96.623370, 7), (29, 51))

    def test_tile_latitude_is_clamped_to_web_mercator_limits(self):
        self.assertEqual(_tile_xy(90, 0, 7), _tile_xy(85.0511, 0, 7))

    def test_optional_status_feed_does_not_break_aircraft_polling(self):
        with mock.patch("flight_telemetry._json", side_effect=OSError("404")):
            self.assertEqual(_optional_json("http://receiver/status.json"), {})

    def test_piaware_fetch_falls_back_when_mdns_is_unavailable(self):
        service = FlightTelemetryService()
        service._base_index = next(
            index for index, base in enumerate(PIAWARE_BASES) if ".local" in base
        )
        def response(url, timeout=3):
            if ".local" in url:
                raise OSError("name resolution failed")
            return {"source": url}
        with mock.patch("flight_telemetry._json", side_effect=response):
            payload = service._fetch("/skyaware/data/aircraft.json")
        fallback_index = next(
            index for index, base in enumerate(PIAWARE_BASES) if ".local" not in base
        )
        self.assertIn(PIAWARE_BASES[fallback_index], payload["source"])
        self.assertEqual(service._base_index, fallback_index)


if __name__ == "__main__":
    unittest.main()
