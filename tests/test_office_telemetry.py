import os
import json
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from office_telemetry import (TelemetryService, _smartthings_summary,
                              _tempest_cloud_summary, _water_summary)


class LocalTelemetryParsingTests(unittest.TestCase):
    def test_smartthings_switch_and_level(self):
        payload = [
            {"switch": {"value": "on", "timestamp": "2026-10-05T20:00:00Z"}},
            {"level": {"value": 50, "unit": "%", "timestamp": "2026-10-05T20:00:01Z"}},
        ]
        result = _smartthings_summary(payload)
        self.assertEqual(result["switch"], "on")
        self.assertEqual(result["level"], 50)
        self.assertEqual(result["level_unit"], "%")

    def test_smartthings_battery_inventory_and_named_overflow_sensor(self):
        payload = [
            {"water": {"value": "dry", "timestamp": "2026-10-07T20:00:00Z"}},
            {"battery": {"value": 22, "unit": "%", "timestamp": "2026-10-07T19:00:00Z"}},
        ]
        service = TelemetryService()
        service._mqtt_message(None, None, SimpleNamespace(
            topic="smartthings/Attic AC Overflow", payload=json.dumps(payload).encode()))
        snapshot = service.snapshot()
        self.assertEqual(snapshot["house"]["leaks"]["Attic AC Overflow"]["state"], "dry")
        battery = snapshot["house"]["batteries"]["Attic AC Overflow"]
        self.assertEqual(battery["level_pct"], 22)
        self.assertEqual(battery["value_text"], "22%")

    def test_usgs_lake_elevation_code(self):
        payload = {"value": {"timeSeries": [{
            "variable": {"variableCode": [{"value": "62614"}]},
            "values": [{"value": [{"value": "518.57", "dateTime": "2026-10-05T20:15:00-05:00"}]}],
        }]}}
        result = _water_summary("LewisvilleLake", payload, 1)
        self.assertTrue(result["raw_ok"])
        self.assertAlmostEqual(result["elevation_ft"], 518.57)

    def test_usgs_trinity_flow_and_gage(self):
        payload = {"value": {"timeSeries": [
            {"variable": {"variableCode": [{"value": "00060"}]},
             "values": [{"value": [{"value": "231", "dateTime": "2026-10-05T20:30:00-05:00"}]}]},
            {"variable": {"variableCode": [{"value": "00065"}]},
             "values": [{"value": [{"value": "5.62", "dateTime": "2026-10-05T20:30:00-05:00"}]}]},
        ]}}
        result = _water_summary("TrinityRiver", payload, 1)
        self.assertEqual(result["flow_cfs"], 231)
        self.assertAlmostEqual(result["gage_ft"], 5.62)

    def test_error_text_is_not_rendered_as_zero(self):
        result = _water_summary("TrinityRiver", "ReadError: request aborted", 1)
        self.assertFalse(result["raw_ok"])
        self.assertNotIn("flow_cfs", result)

    def test_tempest_five_minute_lightning_window(self):
        service = TelemetryService()
        base = 1_791_394_700
        for offset, count, distance in ((0, 1, 16), (60, 2, 20), (360, 3, 8)):
            observation = [base + offset, 0, 0, 0, 0, 60, 997, 27, 43, 100,
                           1, 20, 0, 0, distance, count, 2.4, 1]
            service._handle_weather({"type": "obs_st", "serial_number": "ST-TEST",
                                     "hub_sn": "HB-TEST", "obs": [observation]})
        weather = service.snapshot()["weather"]
        self.assertEqual(weather["lightning_5m"], 5)
        self.assertAlmostEqual(weather["lightning_5m_km"], 14)

    def test_tempest_strike_event_updates_last_distance_without_double_count(self):
        service = TelemetryService()
        service._handle_weather({"type": "evt_strike", "evt": [1_791_394_700, 12, 500]})
        weather = service.snapshot()["weather"]
        self.assertEqual(weather["last_lightning_km"], 12)
        self.assertNotIn("lightning_5m", weather)

    def test_tempest_close_strike_records_warning_timestamp_and_distance(self):
        service = TelemetryService()
        service._handle_weather({"type": "evt_strike", "evt": [1_791_394_700, 0.5, 500]})
        weather = service.snapshot()["weather"]
        self.assertEqual(weather["last_close_lightning"], 1_791_394_700)
        self.assertEqual(weather["last_close_lightning_km"], 0.5)

    def test_tempest_cloud_daily_rain_and_rate(self):
        observation = [1_791_394_700, 0, 0, 0, 0, 60, 997, 27, 43, 100,
                       1, 20, .5, 1, 0, 0, 2.4, 1, 3.5, None, None, 0]
        result = _tempest_cloud_summary({"type": "obs_st", "obs": [observation]}, 10)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["rain_rate_mmh"], 30)
        self.assertAlmostEqual(result["local_day_rain_mm"], 3.5)
        self.assertEqual(result["rain_source"], "TEMPEST")

    def test_tempest_cloud_prefers_visible_nearcast_total(self):
        observation = [1_791_394_700, 0, 0, 0, 0, 60, 997, 27, 43, 100,
                       1, 20, .5, 1, 0, 0, 2.4, 1, 3.5, .6, 4.25, 1]
        result = _tempest_cloud_summary({"type": "obs_st", "obs": [observation]}, 10)
        self.assertAlmostEqual(result["local_day_rain_mm"], 4.25)
        self.assertEqual(result["rain_source"], "NEARCAST")


if __name__ == "__main__":
    unittest.main()
