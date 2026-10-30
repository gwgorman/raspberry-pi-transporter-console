#!/usr/bin/env python3
"""Read-only PiAware/SkyAware telemetry for the transporter console."""

import collections
import copy
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request


PIAWARE_BASES = tuple(dict.fromkeys(
    base.strip().rstrip("/") for base in os.environ.get(
        "STARTREK_PIAWARE_BASES",
        "http://192.168.4.46,http://piawareoutside2.local").split(",")
    if base.strip()))
NEXRAD_TEMPLATE = ("http://mesonet1.agron.iastate.edu/cache/tile.py/1.0.0/"
                   "nexrad-n0q-900913/{z}/{x}/{y}.png")


def _json(url, timeout=3):
    request = urllib.request.Request(url, headers={"User-Agent": "startrek-console/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _optional_json(url, timeout=3):
    """Read supplementary receiver metadata without breaking live tracks."""
    try:
        return _json(url, timeout=timeout)
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def _bytes(url, timeout=5):
    request = urllib.request.Request(url, headers={"User-Agent": "startrek-console/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _tile_xy(lat, lon, zoom):
    scale = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * scale)
    latitude = math.radians(max(-85.0511, min(85.0511, lat)))
    y = int((1.0 - math.asinh(math.tan(latitude)) / math.pi) / 2.0 * scale)
    return x, y


class FlightTelemetryService:
    """Poll local ADS-B data, resolve local type metadata, and cache NEXRAD tiles."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._weather_thread = None
        self._weather_enabled = False
        self._type_cache = {}
        self._base_index = 0
        self._tracks = collections.defaultdict(lambda: collections.deque(maxlen=90))
        self._data = {
            "updated": 0.0,
            "connected": False,
            "error": "STARTING",
            "receiver": {},
            "status": {},
            "aircraft": [],
            "weather": {"enabled": False, "updated": 0.0, "tiles": {},
                        "zoom": 7, "error": "OFF"},
        }

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="ship-piaware", daemon=True)
        self._thread.start()
        self._weather_thread = threading.Thread(target=self._weather_loop,
                                                name="ship-nexrad", daemon=True)
        self._weather_thread.start()

    def stop(self):
        self._stop.set()

    def set_weather_enabled(self, enabled):
        with self._lock:
            self._weather_enabled = bool(enabled)
            self._data["weather"]["enabled"] = bool(enabled)
            if not enabled:
                self._data["weather"]["error"] = "OFF"

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def _fetch(self, path, timeout=3, optional=False):
        """Prefer mDNS, but rotate through configured last-known-good bases."""
        errors = []
        count = len(PIAWARE_BASES)
        for offset in range(count):
            index = (self._base_index + offset) % count
            base = PIAWARE_BASES[index]
            try:
                payload = _json(base + path, timeout=timeout)
                self._base_index = index
                return payload
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"{base}: {exc}")
        if optional:
            return {}
        raise OSError("; ".join(errors)[-240:] or "NO PIAWARE BASE CONFIGURED")

    def _lookup_aircraft(self, icao):
        icao = str(icao or "").upper()
        if not icao:
            return {}
        if icao in self._type_cache:
            return self._type_cache[icao]
        try:
            for level in range(1, len(icao)):
                key, remainder = icao[:level], icao[level:]
                payload = self._fetch(f"/skyaware/db/{key}.json", timeout=2)
                if remainder in payload:
                    result = payload[remainder]
                    self._type_cache[icao] = result
                    return result
                children = payload.get("children", [])
                if key + remainder[:1] not in children:
                    break
        except (OSError, ValueError, urllib.error.URLError):
            pass
        self._type_cache[icao] = {}
        return {}

    def _refresh_weather(self, receiver):
        now = time.time()
        with self._lock:
            weather = self._data["weather"]
            enabled = self._weather_enabled
            due = now - weather.get("updated", 0) >= 300
        if not enabled or not due or receiver.get("lat") is None or receiver.get("lon") is None:
            return
        zoom = 7
        center_x, center_y = _tile_xy(float(receiver["lat"]), float(receiver["lon"]), zoom)
        tiles = {}
        error = ""
        try:
            for x in range(center_x - 1, center_x + 2):
                for y in range(center_y - 1, center_y + 2):
                    tiles[(x, y)] = _bytes(NEXRAD_TEMPLATE.format(z=zoom, x=x, y=y))
        except (OSError, urllib.error.URLError) as exc:
            error = str(exc)
        with self._lock:
            weather = self._data["weather"]
            weather.update(updated=now, zoom=zoom, error=error or "")
            if tiles:
                weather["tiles"] = tiles

    def _weather_loop(self):
        while not self._stop.wait(2.0):
            with self._lock:
                receiver = dict(self._data.get("receiver", {}))
                enabled = self._weather_enabled
            if enabled:
                self._refresh_weather(receiver)

    def _loop(self):
        receiver_refresh = status_refresh = 0.0
        receiver = {}
        status = {}
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - receiver_refresh > 60 or not receiver:
                    receiver = self._fetch("/skyaware/data/receiver.json")
                    receiver_refresh = now
                if now - status_refresh > 10 or not status:
                    status = self._fetch("/status.json", optional=True)
                    status_refresh = now
                payload = self._fetch("/skyaware/data/aircraft.json")
                aircraft = []
                new_lookups = 0
                def lookup_priority(item):
                    if item.get("lat") is None or item.get("lon") is None:
                        return float("inf")
                    dx = ((float(item["lon"]) - float(receiver.get("lon", 0))) *
                          math.cos(math.radians(float(receiver.get("lat", 0)))))
                    dy = float(item["lat"]) - float(receiver.get("lat", 0))
                    return math.hypot(dx, dy)
                raw_aircraft = sorted(payload.get("aircraft", []), key=lookup_priority)
                for raw in raw_aircraft:
                    item = dict(raw)
                    identifier = str(item.get("hex") or "").upper()
                    if identifier in self._type_cache:
                        metadata = self._type_cache[identifier]
                    elif new_lookups < 2:
                        metadata = self._lookup_aircraft(identifier)
                        new_lookups += 1
                    else:
                        metadata = {}
                    item["type"] = metadata.get("t")
                    item["registration"] = metadata.get("r")
                    item["description"] = metadata.get("desc")
                    if item.get("lat") is not None and item.get("lon") is not None:
                        track = self._tracks[item.get("hex")]
                        position = (float(item["lat"]), float(item["lon"]), now)
                        if not track or now - track[-1][2] >= 2:
                            track.append(position)
                        item["trail"] = list(track)
                    aircraft.append(item)
                active = {item.get("hex") for item in aircraft}
                for identifier in list(self._tracks):
                    if identifier not in active or (self._tracks[identifier] and
                                                     now - self._tracks[identifier][-1][2] > 180):
                        del self._tracks[identifier]
                with self._lock:
                    self._data.update(updated=now, connected=True, error="",
                                      receiver=receiver, status=status, aircraft=aircraft,
                                      messages=payload.get("messages", 0), source_now=payload.get("now"))
            except (OSError, ValueError, urllib.error.URLError) as exc:
                with self._lock:
                    self._data.update(connected=False, error=str(exc), updated=now)
            self._stop.wait(1.0)
