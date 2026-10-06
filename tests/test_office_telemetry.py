import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from office_telemetry import _smartthings_summary, _water_summary


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


if __name__ == "__main__":
    unittest.main()
