"""Read-only local telemetry providers for the transporter Ship Status screen."""

from __future__ import annotations

import base64
import collections
import copy
import datetime as dt
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

try:
    import psutil
except ImportError:  # The console remains functional without telemetry extras.
    psutil = None

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


MQTT_HOST = "snoop433.local"
MQTT_PORT = 1883
SMARTTHINGS_CONFIG = os.path.expanduser("~/.config/startrek-smartthings.json")
SMARTTHINGS_API = "https://api.smartthings.com/v1"
SMARTTHINGS_TOKEN_URL = "https://api.smartthings.com/v1/oauth/token"
SMARTTHINGS_DEFAULT_POLL_SECONDS = 300
_smartthings_refresh_lock = threading.Lock()
WEATHERFLOW_PORT = 50222
TEMPEST_CONFIG = os.path.expanduser("~/.config/startrek-tempest.json")
TEMPEST_REFRESH_SECONDS = 60
USGS_REFRESH_SECONDS = 900
USGS_URLS = {
    "lake": ("LewisvilleLake", "https://waterservices.usgs.gov/nwis/iv/"
             "?format=json&sites=08052800&siteStatus=all"),
    "trinity": ("TrinityRiver", "https://waterservices.usgs.gov/nwis/iv/"
                "?format=json&sites=08053000&parameterCd=00060%2C00065&siteStatus=all"),
}

HOUSE_TOPICS = (
    "HourlyForecast",
    "smartthings/wine cellar temp",
    "smartthings/Keg",
    "smartthings/L garage door",
    "smartthings/R garage door",
    "smartthings/Basement",
    "smartthings/CineMate",
    "smartthings/Upstairs Water Heater Leak",
    "smartthings/Bar Sink Leak",
    "smartthings/kitchen Sink Leak",
    "smartthings/Washing Machine Water Leak Sensor",
    "smartthings/Attic AC Overflow",
    "smartthings/Ice Maker",
    "LewisvilleLake",
    "TrinityRiver",
    "smartthings/Bar Front",
    "smartthings/Bar Overhead",
    "smartthings/Bar Signs",
    "smartthings/Breakfast Nook",
    "smartthings/Couch 1",
    "smartthings/Couch 2",
    "smartthings/Dining Room",
    "smartthings/Dining Room Chandelier",
    "smartthings/Dining Room Overhead",
    "smartthings/Family Room",
    "smartthings/Family Room Fan Light",
    "smartthings/Family Room Fan Motor",
    "smartthings/Family Room Fireplace Light",
    "smartthings/Family Room Overhead",
    "smartthings/Fence 1",
    "smartthings/Garage",
    "smartthings/Garage Refrigerator",
    "smartthings/Hallway Overhead",
    "smartthings/Kitchen",
    "smartthings/Living Room",
    "smartthings/Main Bedroom",
    "smartthings/Office",
    "smartthings/Patio",
    "smartthings/Patio Lights 1",
    "smartthings/Patio Lights 2",
    "smartthings/Patio Lights 3",
    "smartthings/Patio Speakers",
)

LEAK_TOPICS = {
    "smartthings/Upstairs Water Heater Leak",
    "smartthings/Bar Sink Leak",
    "smartthings/kitchen Sink Leak",
    "smartthings/Washing Machine Water Leak Sensor",
    "smartthings/Attic AC Overflow",
    "smartthings/Ice Maker",
}
LEAK_NAMES = {topic.split("/", 1)[1] for topic in LEAK_TOPICS}

SYSTEM_GROUPS = {
    "Basement": "BASEMENT SONOS", "CineMate": "EXERCISE ROOM BOSE",
    "Bar Front": "BAR", "Bar Overhead": "BAR", "Bar Signs": "BAR",
    "Bar Sink Leak": "BAR",
    "Breakfast Nook": "BREAKFAST NOOK",
    "Couch 1": "COUCH", "Couch 2": "COUCH",
    "Dining Room": "DINING ROOM", "Dining Room Chandelier": "DINING ROOM",
    "Dining Room Overhead": "DINING ROOM",
    "Family Room": "FAMILY ROOM", "Family Room Fan Light": "FAMILY ROOM",
    "Family Room Fan Motor": "FAMILY ROOM", "Family Room Fireplace Light": "FAMILY ROOM",
    "Family Room Overhead": "FAMILY ROOM",
    "Fence 1": "FENCE 1", "Garage": "GARAGE SONOS",
    "Garage Refrigerator": "GARAGE REFRIGERATOR",
    "Hallway Overhead": "HALLWAY", "Patio": "PATIO AUDIO",
    "Kitchen": "KITCHEN SONOS", "Living Room": "LIVING ROOM SONOS",
    "Main Bedroom": "MAIN BEDROOM SONOS", "Office": "OFFICE SONOS",
    "Patio Lights 1": "PATIO 1", "Patio Lights 2": "PATIO 2",
    "Patio Lights 3": "PATIO 3", "Patio Speakers": "PATIO AUDIO",
}


def _iso_epoch(value):
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _capability(payload, name):
    """Find the first SmartThings capability value inside its component list."""
    if not isinstance(payload, list):
        return None, 0.0, None
    for component in payload:
        if not isinstance(component, dict) or name not in component:
            continue
        item = component[name]
        if isinstance(item, dict):
            return item.get("value"), _iso_epoch(item.get("timestamp")), item.get("unit")
    return None, 0.0, None


def _smartthings_summary(payload):
    """Extract only display-safe capability values from a retained device payload."""
    summary = {}
    for capability in ("switch", "level", "playbackStatus", "audioTrackData", "volume", "groupVolume",
                       "mute", "contact", "temperature", "humidity", "water", "battery",
                       "DeviceWatch-DeviceStatus"):
        value, stamp, unit = _capability(payload, capability)
        if value is not None:
            summary[capability] = value
            if unit:
                summary[f"{capability}_unit"] = unit
            summary["updated"] = max(summary.get("updated", 0.0), stamp)
    return summary


SMARTTHINGS_REST_ATTRIBUTES = {
    "switch": ("switch",),
    "switchLevel": ("level",),
    "mediaPlayback": ("playbackStatus",),
    "audioVolume": ("volume",),
    "audioMute": ("mute",),
    "audioTrackData": ("audioTrackData",),
    "contactSensor": ("contact",),
    "temperatureMeasurement": ("temperature",),
    "relativeHumidityMeasurement": ("humidity",),
    "waterSensor": ("water",),
    "battery": ("battery",),
}


def _smartthings_rest_summary(payload):
    """Normalize a SmartThings full-status response into the console schema."""
    summary = {}
    components = payload.get("components", {}) if isinstance(payload, dict) else {}
    for component in components.values():
        if not isinstance(component, dict):
            continue
        for capability, attributes in SMARTTHINGS_REST_ATTRIBUTES.items():
            values = component.get(capability, {})
            if not isinstance(values, dict):
                continue
            for attribute in attributes:
                item = values.get(attribute)
                if not isinstance(item, dict) or item.get("value") is None:
                    continue
                value = item["value"]
                unit = item.get("unit")
                if attribute == "temperature" and str(unit).upper() in ("C", "°C"):
                    value = float(value) * 9 / 5 + 32
                    unit = "F"
                summary[attribute] = value
                if unit:
                    summary[f"{attribute}_unit"] = unit
                summary["updated"] = max(summary.get("updated", 0.0),
                                         _iso_epoch(item.get("timestamp")))
    summary["DeviceWatch-DeviceStatus"] = "online"
    return summary


def _smartthings_api_json(path, token, query=None, timeout=12):
    url = f"{SMARTTHINGS_API}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "startrek-console/1.0",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _write_smartthings_config(config, path=SMARTTHINGS_CONFIG):
    """Atomically persist rotated OAuth credentials with owner-only access."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, indent=2)
            config_file.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _smartthings_access_token(config, path=SMARTTHINGS_CONFIG, now=None):
    """Return a valid token, refreshing OAuth credentials before expiry."""
    token = str(config.get("access_token") or config.get("token") or "").strip()
    if str(config.get("auth_type", "")).lower() != "oauth":
        return token
    required = ("client_id", "client_secret", "refresh_token")
    if not all(str(config.get(key, "")).strip() for key in required):
        return token
    now = time.time() if now is None else now
    if float(config.get("expires_at") or 0) > now + 3600:
        return token
    with _smartthings_refresh_lock:
        try:
            with open(path, encoding="utf-8") as config_file:
                latest = json.load(config_file)
        except (FileNotFoundError, OSError, ValueError):
            latest = dict(config)
        token = str(latest.get("access_token") or latest.get("token") or "").strip()
        if float(latest.get("expires_at") or 0) > now + 3600:
            config.clear()
            config.update(latest)
            return token
        credentials = f"{latest['client_id']}:{latest['client_secret']}".encode("utf-8")
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": latest["refresh_token"],
            "client_id": latest["client_id"],
        }).encode("ascii")
        request = urllib.request.Request(SMARTTHINGS_TOKEN_URL, data=body, headers={
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "startrek-console/1.0",
        })
        with urllib.request.urlopen(request, timeout=15) as response:
            refreshed = json.load(response)
        if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
            raise ValueError("SmartThings token refresh returned incomplete credentials")
        latest.update({
            "access_token": refreshed["access_token"],
            "refresh_token": refreshed["refresh_token"],
            "expires_at": now + int(refreshed.get("expires_in", 86400)),
        })
        if refreshed.get("installed_app_id"):
            latest["installed_app_id"] = refreshed["installed_app_id"]
        latest.pop("token", None)
        _write_smartthings_config(latest, path)
        config.clear()
        config.update(latest)
        return latest["access_token"]


def _walk_mapping(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_mapping(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mapping(child)


def _water_summary(topic, payload, now):
    """Parse known water shapes conservatively; an error is never rendered as zero."""
    result = {"updated": now, "error": "", "raw_ok": False}
    if isinstance(payload, str):
        preview = payload.lower()
        if "error" in preview or "<!doctype" in preview or "aborted" in preview:
            result["error"] = "DATA LINK FAULT"
            return result
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            try:
                number = float(payload.strip())
            except (TypeError, ValueError):
                result["error"] = "UNRECOGNIZED FEED"
                return result
            if topic == "LewisvilleLake":
                result.update(elevation_ft=number, raw_ok=True)
            else:
                result.update(flow_cfs=number, raw_ok=True)
            return result
    if isinstance(payload, (int, float)):
        result.update(raw_ok=True)
        result["elevation_ft" if topic == "LewisvilleLake" else "flow_cfs"] = float(payload)
        return result
    # Handle USGS JSON time-series and simple named-key collectors without guessing
    # which anonymous number represents lake elevation or discharge.
    for mapping in _walk_mapping(payload):
        lowered = {str(key).lower(): value for key, value in mapping.items()}
        for key in ("elevation_ft", "elevation", "lakelevel", "lake_level", "reservoir_elevation"):
            if key in lowered:
                try:
                    result["elevation_ft"] = float(lowered[key])
                    result["raw_ok"] = True
                except (TypeError, ValueError):
                    pass
        for key in ("flow_cfs", "discharge", "streamflow", "flow"):
            if key in lowered:
                try:
                    result["flow_cfs"] = float(lowered[key])
                    result["raw_ok"] = True
                except (TypeError, ValueError):
                    pass
        for key in ("gage_ft", "gageheight", "gage_height", "height"):
            if key in lowered:
                try:
                    result["gage_ft"] = float(lowered[key])
                    result["raw_ok"] = True
                except (TypeError, ValueError):
                    pass
        variable = mapping.get("variable") if isinstance(mapping, dict) else None
        values = mapping.get("values") if isinstance(mapping, dict) else None
        if isinstance(variable, dict) and isinstance(values, list) and values:
            codes = variable.get("variableCode") or []
            code = str(codes[0].get("value", "")) if codes and isinstance(codes[0], dict) else ""
            try:
                sample = values[0].get("value", [])[-1]
                number = float(sample.get("value"))
                stamp = _iso_epoch(sample.get("dateTime"))
                if code == "00060":
                    result["flow_cfs"] = number
                elif code == "00065":
                    result["gage_ft"] = number
                elif code == "62614":
                    result["elevation_ft"] = number
                else:
                    continue
                result["updated"] = stamp or now
                result["raw_ok"] = True
            except (AttributeError, IndexError, TypeError, ValueError):
                pass
    if not result["raw_ok"]:
        result["error"] = "UNRECOGNIZED FEED"
    return result


def _tempest_cloud_summary(payload, received):
    """Normalize an extended cloud obs_st record without exposing credentials."""
    observations = payload.get("obs") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "obs_st" or not observations:
        return {"valid": False, "updated": received, "error": "NO TEMPEST OBSERVATION"}
    ob = observations[-1]
    if len(ob) < 18:
        return {"valid": False, "updated": received, "error": "SHORT TEMPEST OBSERVATION"}
    interval_minutes = float(ob[17] or 1)
    raw_interval = float(ob[12] or 0)
    raw_day = float(ob[18]) if len(ob) > 18 and ob[18] is not None else None
    nearcast_interval = float(ob[19]) if len(ob) > 19 and ob[19] is not None else None
    nearcast_day = float(ob[20]) if len(ob) > 20 and ob[20] is not None else None
    analysis_type = int(ob[21] or 0) if len(ob) > 21 else 0
    use_nearcast = analysis_type == 1 and nearcast_day is not None
    interval_mm = nearcast_interval if use_nearcast and nearcast_interval is not None else raw_interval
    return {
        "valid": True,
        "updated": received,
        "observed": float(ob[0]),
        "rain_rate_mmh": interval_mm * 60 / max(1, interval_minutes),
        "rain_interval_mm": interval_mm,
        "precip_type": int(ob[13] or 0),
        "local_day_rain_mm": nearcast_day if use_nearcast else raw_day,
        "rain_source": "NEARCAST" if use_nearcast else "TEMPEST",
        "analysis_type": analysis_type,
    }


class TelemetryService:
    """Collect system, WeatherFlow, and selected MQTT data off the UI thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_net = {}
        self._lightning_observations = collections.OrderedDict()
        self._data = {
            "updated": 0.0,
            "system": {},
            "network": {},
            "weather": {},
            "forecast": {},
            "house": {"leaks": {}, "systems": {}, "batteries": {}},
            "water": {"lake": {}, "trinity": {}},
            "mqtt": {"connected": False, "updated": 0.0, "error": "STARTING"},
            "smartthings": {"connected": False, "updated": 0.0,
                            "error": "NOT CONFIGURED", "devices": 0},
            "weatherflow": {"connected": False, "updated": 0.0, "error": "WAITING"},
            "tempest_cloud": {"connected": False, "updated": 0.0,
                              "error": "NOT CONFIGURED"},
        }
        self._threads = []

    def start(self):
        if self._threads:
            return
        for target, name in ((self._system_loop, "ship-system-telemetry"),
                             (self._weather_loop, "ship-weatherflow"),
                             (self._tempest_cloud_loop, "ship-tempest-cloud"),
                             (self._smartthings_loop, "ship-smartthings-api"),
                             (self._mqtt_loop, "ship-mqtt"),
                             (self._usgs_loop, "ship-usgs-water")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def _system_loop(self):
        if psutil:
            psutil.cpu_percent(interval=None)
        while not self._stop.wait(1.0):
            now = time.time()
            try:
                if psutil:
                    cpu = psutil.cpu_percent(interval=None)
                    memory = psutil.virtual_memory().percent
                    uptime = now - psutil.boot_time()
                    counters = psutil.net_io_counters(pernic=True)
                    addresses = psutil.net_if_addrs()
                    stats = psutil.net_if_stats()
                else:
                    cpu = memory = 0.0
                    uptime = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
                    counters, addresses, stats = {}, {}, {}
                thermal_path = "/sys/class/thermal/thermal_zone0/temp"
                temperature = float(open(thermal_path, encoding="utf-8").read()) / 1000.0
                network = {}
                for name in ("wlan0", "eth0"):
                    counter = counters.get(name)
                    previous = self._last_net.get(name)
                    rx = counter.bytes_recv if counter else 0
                    tx = counter.bytes_sent if counter else 0
                    elapsed = max(.001, now - previous[0]) if previous else 1.0
                    rx_rate = max(0.0, (rx - previous[1]) / elapsed) if previous else 0.0
                    tx_rate = max(0.0, (tx - previous[2]) / elapsed) if previous else 0.0
                    self._last_net[name] = (now, rx, tx)
                    ipv4 = "—"
                    for address in addresses.get(name, ()):
                        if getattr(address, "family", None) == socket.AF_INET:
                            ipv4 = address.address
                            break
                    link = stats.get(name)
                    network[name] = {
                        "up": bool(link and link.isup), "speed_mbps": int(link.speed) if link else 0,
                        "ipv4": ipv4, "rx_rate": rx_rate, "tx_rate": tx_rate,
                        "rx_total": rx, "tx_total": tx,
                    }
                wireless = self._wireless_details()
                network.setdefault("wlan0", {}).update(wireless)
                with self._lock:
                    self._data["system"] = {"cpu": cpu, "memory": memory,
                                             "temperature_c": temperature, "uptime": uptime}
                    self._data["network"] = network
                    self._data["updated"] = now
            except Exception as exc:
                with self._lock:
                    self._data["system_error"] = str(exc)

    @staticmethod
    def _wireless_details():
        details = {"signal_dbm": None, "ssid": "—"}
        try:
            for line in open("/proc/net/wireless", encoding="utf-8"):
                if "wlan0:" in line:
                    fields = line.replace(".", "").split()
                    details["signal_dbm"] = float(fields[3])
                    break
        except (OSError, ValueError, IndexError):
            pass
        try:
            result = subprocess.run(["iwgetid", "wlan0", "--raw"], capture_output=True,
                                    text=True, timeout=1, check=False)
            if result.stdout.strip():
                details["ssid"] = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return details

    def _weather_loop(self):
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", WEATHERFLOW_PORT))
                sock.settimeout(2)
                with self._lock:
                    self._data["weatherflow"]["error"] = "WAITING"
                while not self._stop.is_set():
                    try:
                        packet = json.loads(sock.recvfrom(65535)[0].decode("utf-8"))
                        self._handle_weather(packet)
                    except socket.timeout:
                        continue
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
            except OSError as exc:
                with self._lock:
                    self._data["weatherflow"].update(connected=False, error=str(exc))
                self._stop.wait(5)
            finally:
                if sock:
                    sock.close()

    def _merge_water(self, source, summary, now=None):
        now = now or time.time()
        with self._lock:
            previous = self._data["water"].get(source, {})
            if not summary.get("raw_ok") and previous.get("raw_ok"):
                previous.update(error=summary.get("error", "DATA LINK FAULT"),
                                link_updated=summary.get("updated", now))
            else:
                summary["link_updated"] = now
                self._data["water"][source] = summary

    def _usgs_loop(self):
        """Poll primary USGS feeds directly; MQTT remains a local fallback."""
        while not self._stop.is_set():
            for source, (topic, url) in USGS_URLS.items():
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "startrek-console/1.0"})
                    with urllib.request.urlopen(request, timeout=15) as response:
                        payload = json.load(response)
                    summary = _water_summary(topic, payload, time.time())
                    summary["source"] = "USGS DIRECT"
                except Exception:
                    summary = {"raw_ok": False, "error": "USGS LINK FAULT",
                               "updated": time.time(), "source": "USGS DIRECT"}
                self._merge_water(source, summary)
            self._stop.wait(USGS_REFRESH_SECONDS)

    def _tempest_cloud_loop(self):
        """Poll the authoritative cloud observation once per report interval."""
        while not self._stop.is_set():
            try:
                if os.stat(TEMPEST_CONFIG).st_mode & 0o077:
                    raise PermissionError("CONFIG FILE MUST USE MODE 0600")
                with open(TEMPEST_CONFIG, encoding="utf-8") as stream:
                    config = json.load(stream)
                if not config.get("enabled") or not config.get("token") or not config.get("device_id"):
                    raise PermissionError("TEMPEST CREDENTIALS INCOMPLETE")
                query = urllib.parse.urlencode({"device_id": int(config["device_id"]),
                                                "token": config["token"]})
                request = urllib.request.Request(
                    f"https://swd.weatherflow.com/swd/rest/observations/?{query}",
                    headers={"User-Agent": "startrek-console/1.0"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    summary = _tempest_cloud_summary(json.load(response), time.time())
                if not summary.get("valid"):
                    raise ValueError(summary.get("error", "TEMPEST DATA INVALID"))
                with self._lock:
                    self._data["weather"].update(summary)
                    self._data["tempest_cloud"].update(
                        connected=True, updated=summary["updated"], error="",
                        station_id=config.get("station_id"), device_id=config.get("device_id"))
            except FileNotFoundError:
                with self._lock:
                    self._data["tempest_cloud"].update(connected=False, error="NOT CONFIGURED")
            except Exception as exc:
                with self._lock:
                    self._data["tempest_cloud"].update(
                        connected=False, error=type(exc).__name__)
            self._stop.wait(TEMPEST_REFRESH_SECONDS)

    def _handle_weather(self, packet):
        kind, now = packet.get("type"), time.time()
        values = {}
        if kind == "rapid_wind" and len(packet.get("ob", ())) >= 3:
            ob = packet["ob"]
            values = {"wind_mps": ob[1], "wind_direction": ob[2], "observed": ob[0]}
        elif kind == "obs_st" and packet.get("obs"):
            ob = packet["obs"][0]
            if len(ob) >= 18:
                observed = float(ob[0])
                strike_count = max(0, int(ob[15] or 0))
                strike_distance = float(ob[14]) if strike_count and ob[14] is not None else None
                self._lightning_observations[observed] = (strike_count, strike_distance)
                cutoff = observed - 300
                while self._lightning_observations:
                    timestamp = next(iter(self._lightning_observations))
                    if timestamp >= cutoff:
                        break
                    self._lightning_observations.popitem(last=False)
                lightning_5m = sum(item[0] for item in self._lightning_observations.values())
                recent_distances = [item[1] for item in self._lightning_observations.values()
                                    if item[0] and item[1] is not None]
                values = {
                    "observed": ob[0], "wind_lull_mps": ob[1], "wind_mps": ob[2],
                    "wind_gust_mps": ob[3], "wind_direction": ob[4], "pressure_mb": ob[6],
                    "temperature_c": ob[7], "humidity": ob[8], "illuminance": ob[9],
                    "uv": ob[10], "solar_wm2": ob[11], "rain_mm": ob[12],
                    "rain_rate_mmh": float(ob[12] or 0) * 60 / max(1, float(ob[17] or 1)),
                    "precip_type": ob[13], "lightning_km": ob[14],
                    "lightning_count": strike_count, "lightning_5m": lightning_5m,
                    "lightning_5m_km": (sum(recent_distances) / len(recent_distances)
                                         if recent_distances else None),
                    "battery_v": ob[16], "station_serial": packet.get("serial_number"),
                    "hub_serial": packet.get("hub_sn"),
                }
                if strike_distance is not None and strike_distance <= 0.804672:
                    values.update(last_close_lightning=observed,
                                  last_close_lightning_km=strike_distance)
                if len(ob) > 18:
                    values["daily_rain_mm"] = ob[18]
        elif kind == "evt_strike" and len(packet.get("evt", ())) >= 2:
            values = {"last_lightning": packet["evt"][0],
                      "last_lightning_km": packet["evt"][1]}
            if float(packet["evt"][1]) <= 0.804672:
                values.update(last_close_lightning=packet["evt"][0],
                              last_close_lightning_km=packet["evt"][1])
        elif kind == "evt_precip":
            values = {"precip_active": True, "last_precip": packet.get("evt", [now])[0]}
        if values:
            with self._lock:
                self._data["weather"].update(values)
                self._data["weatherflow"].update(connected=True, updated=now, error="")

    def _apply_smartthings_summary(self, name, summary, now, source):
        """Merge one normalized SmartThings device without changing UI schemas."""
        with self._lock:
            house = self._data["house"]
            if summary.get("battery") is not None:
                level = float(summary["battery"])
                house.setdefault("batteries", {})[name] = {
                    "name": name, "source": source, "level_pct": level,
                    "value_text": f"{level:.0f}%",
                    "reported_at": summary.get("updated") or now,
                    "activity_at": summary.get("updated") or now,
                    "online": summary.get("DeviceWatch-DeviceStatus", "online"),
                    "unit": summary.get("battery_unit", "%"),
                }
            if name.casefold() in ("wine cellar temp", "keg"):
                key = "wine_cellar" if "wine" in name.casefold() else "keg"
                house[key] = {
                    "temperature_f": summary.get("temperature"),
                    "humidity": summary.get("humidity"),
                    "updated": summary.get("updated") or now,
                }
            if name.casefold() in ("l garage door", "r garage door"):
                key = "garage_left" if name.casefold().startswith("l ") else "garage_right"
                house[key] = {"state": summary.get("contact"),
                              "updated": summary.get("updated") or now}
            if name in LEAK_NAMES:
                house.setdefault("leaks", {})[name] = {
                    "state": summary.get("water"), "updated": summary.get("updated") or now}
            if name in SYSTEM_GROUPS:
                system = dict(summary, name=name, group=SYSTEM_GROUPS[name], received=now)
                house.setdefault("systems", {})[name] = system

    def _smartthings_loop(self):
        wanted_names = (set(SYSTEM_GROUPS) | LEAK_NAMES |
                        {"wine cellar temp", "Keg", "L garage door", "R garage door"})
        canonical = {name.casefold(): name for name in wanted_names}
        while not self._stop.is_set():
            try:
                with open(SMARTTHINGS_CONFIG, encoding="utf-8") as config_file:
                    config = json.load(config_file)
            except FileNotFoundError:
                with self._lock:
                    self._data["smartthings"].update(
                        connected=False, error="NOT CONFIGURED")
                self._stop.wait(30)
                continue
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._data["smartthings"].update(connected=False, error=str(exc))
                self._stop.wait(30)
                continue
            if not config.get("enabled"):
                with self._lock:
                    self._data["smartthings"].update(
                        connected=False, error="NOT CONFIGURED")
                self._stop.wait(30)
                continue
            poll_seconds = max(60, int(config.get(
                "poll_seconds", SMARTTHINGS_DEFAULT_POLL_SECONDS)))
            try:
                token = _smartthings_access_token(config)
                if not token:
                    raise ValueError("SmartThings access token is missing")
                query = {"locationId": config["location_id"]} if config.get("location_id") else None
                devices = _smartthings_api_json("/devices", token, query).get("items", [])
                selected = []
                for device in devices:
                    label = str(device.get("label") or device.get("name") or "").strip()
                    selected_name = canonical.get(label.casefold())
                    if selected_name and device.get("deviceId"):
                        selected.append((selected_name, device["deviceId"]))
                successful = 0
                errors = []
                for name, device_id in selected:
                    if self._stop.is_set():
                        break
                    try:
                        status = _smartthings_api_json(f"/devices/{device_id}/status", token)
                        summary = _smartthings_rest_summary(status)
                        self._apply_smartthings_summary(name, summary, time.time(),
                                                        "SMARTTHINGS API")
                        successful += 1
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")
                now = time.time()
                with self._lock:
                    self._data["smartthings"].update(
                        connected=bool(successful), updated=now,
                        devices=successful,
                        error=(f"{len(errors)} DEVICE ERRORS" if errors else
                               "NO MATCHING DEVICES" if not selected else ""))
            except Exception as exc:
                with self._lock:
                    self._data["smartthings"].update(connected=False, error=str(exc))
            self._stop.wait(poll_seconds)

    def _mqtt_loop(self):
        if mqtt is None:
            with self._lock:
                self._data["mqtt"].update(error="PAHO MQTT NOT INSTALLED", connected=False)
            return
        while not self._stop.is_set():
            try:
                try:
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                         client_id="startrek-ship-status")
                except AttributeError:
                    client = mqtt.Client(client_id="startrek-ship-status")
                client.on_connect = self._mqtt_connect
                client.on_disconnect = self._mqtt_disconnect
                client.on_message = self._mqtt_message
                client.connect(MQTT_HOST, MQTT_PORT, 20)
                client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                with self._lock:
                    self._data["mqtt"].update(error=str(exc), connected=False)
                self._stop.wait(5)

    def _mqtt_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        connected = (not reason_code.is_failure) if hasattr(reason_code, "is_failure") else reason_code == 0
        with self._lock:
            self._data["mqtt"].update(connected=connected,
                                      error="" if connected else f"CONNACK {reason_code}",
                                      updated=time.time())
        if connected:
            # The wildcard provides a complete retained battery inventory; the
            # explicit list remains for non-SmartThings local feeds.
            client.subscribe("smartthings/#")
            client.subscribe([(topic, 0) for topic in HOUSE_TOPICS
                              if not topic.startswith("smartthings/")])

    def _mqtt_disconnect(self, _client, _userdata, *callback_args):
        reason_code = callback_args[-2] if len(callback_args) >= 2 else callback_args[-1] if callback_args else 0
        with self._lock:
            self._data["mqtt"].update(connected=False, error=f"DISCONNECTED {reason_code}")

    def _mqtt_message(self, _client, _userdata, message):
        now = time.time()
        try:
            text_payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        try:
            payload = json.loads(text_payload)
        except json.JSONDecodeError:
            payload = text_payload
        topic = message.topic
        if topic.startswith("smartthings/"):
            with self._lock:
                api = dict(self._data.get("smartthings", {}))
            if api.get("connected") and time.time() - api.get("updated", 0) < 900:
                return
        updates = {}
        if topic.startswith("smartthings/"):
            name = topic.split("/", 1)[1]
            summary = _smartthings_summary(payload)
            if summary.get("battery") is not None:
                battery_value, battery_stamp, battery_unit = _capability(payload, "battery")
                updates["battery"] = (name, {
                    "name": name,
                    "source": "SMARTTHINGS",
                    "level_pct": float(battery_value),
                    "value_text": f"{float(battery_value):.0f}%",
                    "reported_at": battery_stamp,
                    "activity_at": summary.get("updated", 0.0),
                    "online": summary.get("DeviceWatch-DeviceStatus"),
                    "unit": battery_unit or "%",
                })
        if topic == "HourlyForecast" and isinstance(payload, list) and payload:
            item = payload[0]
            updates = {"forecast": {"temperature_f": item.get("temperature"),
                                     "summary": item.get("shortForecast", "—"),
                                     "precip_percent": (item.get("probabilityOfPrecipitation") or {}).get("value"),
                                     "wind": f"{item.get('windSpeed', '—')} {item.get('windDirection', '')}".strip(),
                                     "updated": now}}
        elif topic in ("smartthings/wine cellar temp", "smartthings/Keg"):
            temperature, stamp, _ = _capability(payload, "temperature")
            humidity, humidity_stamp, _ = _capability(payload, "humidity")
            key = "wine_cellar" if "wine cellar" in topic else "keg"
            updates["house"] = {key: {"temperature_f": temperature, "humidity": humidity,
                                      "updated": max(stamp, humidity_stamp)}}
        elif topic in ("smartthings/L garage door", "smartthings/R garage door"):
            contact, stamp, _ = _capability(payload, "contact")
            key = "garage_left" if "/L garage" in topic else "garage_right"
            updates["house"] = {key: {"state": contact, "updated": stamp}}
        elif topic in LEAK_TOPICS:
            water, stamp, _ = _capability(payload, "water")
            updates["leak"] = (topic.split("/", 1)[-1], water, stamp)
        if topic in ("LewisvilleLake", "TrinityRiver"):
            updates["water_source"] = ("lake" if topic == "LewisvilleLake" else "trinity",
                                       _water_summary(topic, payload, now))
        elif topic.startswith("smartthings/"):
            name = topic.split("/", 1)[1]
            if name in SYSTEM_GROUPS:
                updates["system"] = (name, SYSTEM_GROUPS[name], _smartthings_summary(payload))
        with self._lock:
            if "forecast" in updates:
                self._data["forecast"].update(updates["forecast"])
            if "house" in updates:
                self._data["house"].update(updates["house"])
            if "leak" in updates:
                name, state, stamp = updates["leak"]
                self._data["house"].setdefault("leaks", {})[name] = {
                    "state": state, "updated": stamp}
            if "battery" in updates:
                name, item = updates["battery"]
                self._data["house"].setdefault("batteries", {})[name] = item
            if "system" in updates:
                name, group, summary = updates["system"]
                summary.update(name=name, group=group, received=now)
                self._data["house"].setdefault("systems", {})[name] = summary
            if "water_source" in updates:
                source, summary = updates["water_source"]
                # Do not let a failing local collector overwrite fresh direct USGS data.
                current = self._data["water"].get(source, {})
                if current.get("source") != "USGS DIRECT" or not current.get("raw_ok"):
                    summary["source"] = "LOCAL MQTT"
                    self._merge_water(source, summary, now)
            self._data["mqtt"].update(connected=True, updated=now, error="")
