import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from satellite_telemetry import (
    CACHE_MAX_AGE_SECONDS,
    CACHE_WARNING_SECONDS,
    _dedupe_records,
    _tle_records,
    catalog_condition,
)


SAMPLE = """ISS (ZARYA)
1 25544U 98067A   26280.50000000  .00010000  00000-0  18000-3 0  9991
2 25544  51.6400 120.0000 0005000  40.0000 320.0000 15.50000000123456
HUBBLE SPACE TELESCOPE
1 20580U 90037B   26280.50000000  .00001000  00000-0  60000-4 0  9992
2 20580  28.4700 220.0000 0002500 100.0000 260.0000 15.09000000123457
"""


class SatelliteTelemetryTests(unittest.TestCase):
    def test_tle_parser_extracts_complete_named_records(self):
        records = _tle_records(SAMPLE)
        self.assertEqual([record[3] for record in records], ["25544", "20580"])
        self.assertEqual(records[0][0], "ISS (ZARYA)")

    def test_catalog_deduplicates_by_norad_id(self):
        records = _tle_records(SAMPLE)
        self.assertEqual(len(_dedupe_records(records + records[:1])), 2)

    def test_catalog_health_degrades_to_cache_before_fault(self):
        now = 1_000_000.0
        data = {"connected": True, "catalog_count": 2,
                "catalog_updated": now - 60, "error": ""}
        self.assertEqual(catalog_condition(data, now)[0], "nominal")
        data["error"] = "temporary download failure"
        self.assertEqual(catalog_condition(data, now)[0], "stale")
        data["error"] = ""
        data["catalog_updated"] = now - CACHE_WARNING_SECONDS
        self.assertEqual(catalog_condition(data, now)[0], "stale")
        data["catalog_updated"] = now - CACHE_MAX_AGE_SECONDS
        self.assertEqual(catalog_condition(data, now)[0], "fault")

    def test_catalog_without_cached_records_is_faulted(self):
        condition, _age = catalog_condition(
            {"connected": False, "catalog_count": 0, "catalog_updated": 0}, 100.0)
        self.assertEqual(condition, "fault")


if __name__ == "__main__":
    unittest.main()
