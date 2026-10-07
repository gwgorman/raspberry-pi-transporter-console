#!/usr/bin/env python3
"""Read-only CelesTrak orbital telemetry for the transporter console."""

import copy
import datetime as dt
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    from skyfield.api import EarthSatellite, Loader, Star, wgs84
except ImportError:  # Keep the console usable while the optional package is absent.
    EarthSatellite = Loader = Star = wgs84 = None


SITE_LAT = 33.142790
SITE_LON = -96.623370
REFRESH_SECONDS = 2 * 60 * 60
PASS_REFRESH_SECONDS = 15 * 60
CELESTRAK_URLS = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=VISUAL&FORMAT=TLE",
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=STATIONS&FORMAT=TLE",
)
SATCAT_URLS = (
    "https://celestrak.org/satcat/records.php?GROUP=VISUAL&FORMAT=JSON",
    "https://celestrak.org/satcat/records.php?GROUP=STATIONS&FORMAT=JSON",
)
LAUNCH_COUNTRIES = {
    "AFETR": "USA", "AFWTR": "USA", "ANDSP": "NORWAY", "ALCLC": "BRAZIL",
    "BOS": "AUSTRALIA", "CAS": "CANARIES", "DLS": "RUSSIA", "ERAS": "USA",
    "FRGUI": "FRENCH GUIANA", "HGSTR": "ALGERIA", "JJSLA": "S. KOREA",
    "JSC": "CHINA", "KODAK": "USA", "KSCUT": "JAPAN", "KWAJ": "USA",
    "KYMSC": "RUSSIA", "NSC": "S. KOREA", "PLMSC": "RUSSIA",
    "RLLB": "NEW ZEALAND", "SCSLA": "CHINA", "SEML": "IRAN", "SEMLS": "IRAN",
    "SMTS": "IRAN", "SNMLP": "KENYA", "SPKII": "JAPAN", "SRILR": "INDIA",
    "STARB": "USA", "SVOBO": "RUSSIA", "TAISC": "CHINA", "TANSC": "JAPAN",
    "TYMSC": "KAZAKHSTAN", "VOSTO": "RUSSIA", "WLPIS": "USA",
    "WOMRA": "AUSTRALIA", "WRAS": "USA", "WSC": "CHINA", "XICLF": "CHINA",
    "YAVNE": "ISRAEL", "YSLA": "CHINA", "YUN": "N. KOREA",
    "SEAL": "SEA PLATFORM", "SUBL": "SUBMARINE", "UNK": "UNKNOWN",
}
BRIGHT_STARS = (
    ("SIRIUS", 6.75248, -16.7161, -1.46),
    ("CANOPUS", 6.39920, -52.6957, -0.74),
    ("ARCTURUS", 14.2610, 19.1824, -0.05),
    ("VEGA", 18.6156, 38.7837, 0.03),
    ("CAPELLA", 5.27815, 45.9980, 0.08),
    ("RIGEL", 5.24230, -8.2016, 0.13),
)
PLANETS = (
    ("MERCURY", "mercury"), ("VENUS", "venus"), ("MARS", "mars"),
    ("JUPITER", "jupiter barycenter"), ("SATURN", "saturn barycenter"),
    ("URANUS", "uranus barycenter"), ("NEPTUNE", "neptune barycenter"),
)


def _tle_records(text):
    """Return complete (name, line 1, line 2, catalog id) records."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    index = 0
    while index + 2 < len(lines):
        name, line1, line2 = lines[index:index + 3]
        if line1.startswith("1 ") and line2.startswith("2 "):
            catalog_id = line1[2:7].strip()
            records.append((name.strip(), line1, line2, catalog_id))
            index += 3
        else:
            index += 1
    return records


def _dedupe_records(records):
    unique = {}
    for record in records:
        unique[record[3]] = record
    return list(unique.values())


def _download_text(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": "startrek-console/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _download_json(url, timeout=15):
    return json.loads(_download_text(url, timeout))


class SatelliteTelemetryService:
    """Cache public TLEs and calculate current local sky positions and passes."""

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "startrek-console")
        self.cache_path = os.path.join(self.cache_dir, "celestrak-visual-stations.tle")
        self.metadata_path = os.path.join(self.cache_dir, "celestrak-satcat.json")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._satellites = []
        self._metadata = {}
        self._ts = None
        self._observer = None
        self._earth_observer = None
        self._ephemeris = None
        self._data = {
            "updated": 0.0, "catalog_updated": 0.0, "connected": False,
            "error": "STARTING", "catalog_count": 0, "overhead": [], "upcoming": [],
            "site": {"lat": SITE_LAT, "lon": SITE_LON},
            "planets": [], "stars": [], "celestial_error": "STARTING",
        }

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="ship-orbits", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def _load_records(self, text, stamp):
        records = _dedupe_records(_tle_records(text))
        if not records:
            raise ValueError("CelesTrak returned no complete TLE records")
        self._satellites = [EarthSatellite(line1, line2, name, self._ts)
                            for name, line1, line2, _catalog_id in records]
        with self._lock:
            self._data.update(catalog_updated=stamp, catalog_count=len(self._satellites))

    def _load_cache(self):
        try:
            stamp = os.path.getmtime(self.cache_path)
            with open(self.cache_path, encoding="utf-8") as cache_file:
                self._load_records(cache_file.read(), stamp)
            try:
                with open(self.metadata_path, encoding="utf-8") as metadata_file:
                    self._metadata = json.load(metadata_file)
            except (OSError, ValueError):
                self._metadata = {}
            return stamp
        except OSError:
            return 0.0

    def _refresh_catalog(self):
        chunks = [_download_text(url) for url in CELESTRAK_URLS]
        satcat = {}
        for url in SATCAT_URLS:
            for item in _download_json(url):
                catalog_id = str(item.get("NORAD_CAT_ID") or "")
                launch_site = str(item.get("LAUNCH_SITE") or "UNK").upper()
                satcat[catalog_id] = {
                    "name": item.get("OBJECT_NAME") or "",
                    "owner": item.get("OWNER") or "UNKNOWN",
                    "launch_site": launch_site,
                    "launch_country": LAUNCH_COUNTRIES.get(launch_site, launch_site or "UNKNOWN"),
                    "launch_date": item.get("LAUNCH_DATE") or "",
                }
        records = _dedupe_records(record for chunk in chunks for record in _tle_records(chunk))
        if not records:
            raise ValueError("CelesTrak returned an empty catalog")
        text = "\n".join("\n".join(record[:3]) for record in records) + "\n"
        os.makedirs(self.cache_dir, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="orbits-", suffix=".tle",
                                                 dir=self.cache_dir, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as cache_file:
                cache_file.write(text)
            os.replace(temporary, self.cache_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        stamp = time.time()
        os.utime(self.cache_path, (stamp, stamp))
        metadata_temp = self.metadata_path + ".tmp"
        with open(metadata_temp, "w", encoding="utf-8") as metadata_file:
            json.dump(satcat, metadata_file)
        os.replace(metadata_temp, self.metadata_path)
        self._metadata = satcat
        self._load_records(text, stamp)
        return stamp

    def _positions(self):
        now = self._ts.now()
        overhead = []
        for satellite in self._satellites:
            try:
                altitude, azimuth, distance = (satellite - self._observer).at(now).altaz()
                elevation = float(altitude.degrees)
                if elevation <= 0:
                    continue
                overhead.append({
                    "name": satellite.name, "catalog_id": str(satellite.model.satnum),
                    "azimuth": float(azimuth.degrees), "elevation": elevation,
                    "range_km": float(distance.km),
                    **self._metadata.get(str(satellite.model.satnum), {}),
                })
            except (ValueError, OverflowError):
                continue
        overhead.sort(key=lambda item: item["elevation"], reverse=True)
        planets, stars = [], []
        if self._ephemeris is not None and self._earth_observer is not None:
            for name, key in PLANETS:
                try:
                    altitude, azimuth, _distance = self._earth_observer.at(now).observe(
                        self._ephemeris[key]).apparent().altaz()
                    if altitude.degrees > 0:
                        planets.append({"name": name, "azimuth": float(azimuth.degrees),
                                        "elevation": float(altitude.degrees)})
                except (KeyError, ValueError):
                    continue
            visible_stars = []
            for name, ra_hours, dec_degrees, magnitude in BRIGHT_STARS:
                star = Star(ra_hours=ra_hours, dec_degrees=dec_degrees)
                altitude, azimuth, _distance = self._earth_observer.at(now).observe(
                    star).apparent().altaz()
                if altitude.degrees > 0:
                    visible_stars.append({"name": name, "azimuth": float(azimuth.degrees),
                                          "elevation": float(altitude.degrees),
                                          "magnitude": magnitude})
            stars = sorted(visible_stars, key=lambda item: item["magnitude"])[:3]
        with self._lock:
            self._data.update(updated=time.time(), overhead=overhead, planets=planets,
                              stars=stars, connected=True, error="")

    def _passes(self):
        start = self._ts.now()
        end = self._ts.from_datetime(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12))
        passes = []
        for satellite in self._satellites:
            try:
                times, events = satellite.find_events(self._observer, start, end,
                                                      altitude_degrees=10.0)
                current = None
                for event_time, event in zip(times, events):
                    stamp = event_time.utc_datetime().timestamp()
                    if event == 0:
                        current = {"name": satellite.name,
                                   "catalog_id": str(satellite.model.satnum),
                                   "rise": stamp, "culminate": None, "set": None,
                                   "max_elevation": None}
                        current.update(self._metadata.get(str(satellite.model.satnum), {}))
                    elif event == 1 and current:
                        altitude, _azimuth, _distance = (satellite - self._observer).at(event_time).altaz()
                        current["culminate"] = stamp
                        current["max_elevation"] = float(altitude.degrees)
                    elif event == 2 and current:
                        current["set"] = stamp
                        if current.get("culminate"):
                            passes.append(current)
                        current = None
            except (ValueError, OverflowError):
                continue
        passes.sort(key=lambda item: item["rise"])
        with self._lock:
            self._data["upcoming"] = passes[:12]

    def _loop(self):
        if not EarthSatellite:
            with self._lock:
                self._data["error"] = "PYTHON3-SKYFIELD NOT INSTALLED"
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            loader = Loader(self.cache_dir)
            self._ts = loader.timescale()
            self._observer = wgs84.latlon(SITE_LAT, SITE_LON)
            try:
                self._ephemeris = loader("de421.bsp")
                self._earth_observer = (self._ephemeris["earth"] + self._observer)
                with self._lock:
                    self._data["celestial_error"] = ""
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._data["celestial_error"] = str(exc)
            catalog_stamp = self._load_cache()
        except (OSError, ValueError) as exc:
            catalog_stamp = 0.0
            with self._lock:
                self._data["error"] = str(exc)
        next_pass_refresh = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if not self._satellites or not self._metadata or now - catalog_stamp >= REFRESH_SECONDS:
                    catalog_stamp = self._refresh_catalog()
                    next_pass_refresh = 0.0
                self._positions()
                if now >= next_pass_refresh:
                    self._passes()
                    next_pass_refresh = now + PASS_REFRESH_SECONDS
            except (OSError, ValueError, urllib.error.URLError) as exc:
                with self._lock:
                    self._data.update(error=str(exc), connected=bool(self._satellites))
            self._stop.wait(5.0)
