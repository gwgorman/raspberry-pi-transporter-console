import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from yolink_telemetry import YoLinkService


class YoLinkNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.service = YoLinkService(os.path.join(tempfile.gettempdir(), "missing-yolink-config.json"))
        self.service._config = {"temperature_unit": "C", "shed_device_id": "door-1"}
        self.service._inventory = {
            "temp-1": {"deviceId": "temp-1", "name": "Sun Room", "type": "THSensor", "modelName": "YS8003"},
            "door-1": {"deviceId": "door-1", "name": "Outside Shed", "type": "DoorSensor", "modelName": "YS7704"},
        }

    def test_temperature_report_is_normalized_without_inventing_battery_percent(self):
        self.service._ingest({
            "method": "THSensor.getState",
            "data": {"deviceId": "temp-1", "online": True, "reportAt": "2026-10-05T12:00:00Z",
                     "state": {"temperature": 20.0, "humidity": 51, "battery": 3,
                               "mode": "C", "state": "normal", "alarm": {}}},
        })
        sensor = self.service.snapshot()["temperature_sensors"][0]
        self.assertEqual(sensor["name"], "Sun Room")
        self.assertAlmostEqual(sensor["temperature_f"], 68.0)
        self.assertEqual(sensor["battery"], 3)

    def test_device_fahrenheit_mode_does_not_relabel_celsius_api_value(self):
        self.service._ingest({
            "method": "THSensor.getState",
            "data": {"deviceId": "temp-1", "online": True, "reportAt": "2026-10-05T12:00:00Z",
                     "state": {"temperature": 2.5, "humidity": 45, "battery": 4,
                               "mode": "F", "state": "normal", "alarm": {}}},
        })
        sensor = self.service.snapshot()["temperature_sensors"][0]
        self.assertAlmostEqual(sensor["temperature_f"], 36.5)
        self.assertEqual(sensor["temperature_unit"], "C")

    def test_shed_state_and_state_change_are_distinct_from_report_time(self):
        self.service._ingest({
            "method": "DoorSensor.getState",
            "data": {"deviceId": "door-1", "online": True, "reportAt": "2026-10-05T12:05:00Z",
                     "state": {"state": "open", "battery": 4,
                               "stateChangedAt": "2026-10-05T12:01:00Z"}},
        })
        shed = self.service.snapshot()["shed"]
        self.assertEqual(shed["state"], "open")
        self.assertNotEqual(shed["changed_at"], shed["reported_at"])

    def test_older_reports_do_not_replace_newer_state(self):
        for stamp, state in (("2026-10-05T12:05:00Z", "closed"),
                             ("2026-10-05T12:01:00Z", "open")):
            self.service._ingest({
                "method": "DoorSensor.getState",
                "data": {"deviceId": "door-1", "online": True, "reportAt": stamp,
                         "state": {"state": state, "battery": 4, "stateChangedAt": stamp}},
            })
        self.assertEqual(self.service.snapshot()["shed"]["state"], "closed")


if __name__ == "__main__":
    unittest.main()
