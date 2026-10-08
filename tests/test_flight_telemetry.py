import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flight_telemetry import _optional_json, _tile_xy


class FlightTelemetryTests(unittest.TestCase):
    def test_receiver_location_maps_to_expected_nexrad_tile(self):
        self.assertEqual(_tile_xy(33.142790, -96.623370, 7), (29, 51))

    def test_tile_latitude_is_clamped_to_web_mercator_limits(self):
        self.assertEqual(_tile_xy(90, 0, 7), _tile_xy(85.0511, 0, 7))

    def test_optional_status_feed_does_not_break_aircraft_polling(self):
        with mock.patch("flight_telemetry._json", side_effect=OSError("404")):
            self.assertEqual(_optional_json("http://receiver/status.json"), {})


if __name__ == "__main__":
    unittest.main()
