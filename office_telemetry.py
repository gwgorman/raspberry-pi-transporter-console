"""Read-only local telemetry providers for the transporter Ship Status screen."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import socket
import subprocess
import threading
import time

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
WEATHERFLOW_PORT = 50222

HOUSE_TOPICS = (
    "HourlyForecast",
    "smartthings/wine cellar temp",
    "smartthings/Keg",
    "smartthings/L garage door",
    "smartthings/R garage door",
    "smartthings/Upstairs Water Heater Leak",
    "smartthings/Bar Sink Leak",
    "smartthings/kitchen Sink Leak",
    "smartthings/Washing Machine Water Leak Sensor",
    "smartthings/Attic AC Overflow",
    "smartthings/Centralite Water Leak Sensor",
    "smartthings/Ice Maker",
)


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


class TelemetryService:
    """Collect system, WeatherFlow, and selected MQTT data off the UI thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_net = {}
        self._data = {
            "updated": 0.0,
            "system": {},
            "network": {},
            "weather": {},
            "forecast": {},
            "house": {"leaks": {}},
            "mqtt": {"connected": False, "updated": 0.0, "error": "STARTING"},
            "weatherflow": {"connected": False, "updated": 0.0, "error": "WAITING"},
        }
        self._threads = []

    def start(self):
        if self._threads:
            return
        for target, name in ((self._system_loop, "ship-system-telemetry"),
                             (self._weather_loop, "ship-weatherflow"),
                             (self._mqtt_loop, "ship-mqtt")):
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

    def _handle_weather(self, packet):
        kind, now = packet.get("type"), time.time()
        values = {}
        if kind == "rapid_wind" and len(packet.get("ob", ())) >= 3:
            ob = packet["ob"]
            values = {"wind_mps": ob[1], "wind_direction": ob[2], "observed": ob[0]}
        elif kind == "obs_st" and packet.get("obs"):
            ob = packet["obs"][0]
            if len(ob) >= 18:
                values = {
                    "observed": ob[0], "wind_lull_mps": ob[1], "wind_mps": ob[2],
                    "wind_gust_mps": ob[3], "wind_direction": ob[4], "pressure_mb": ob[6],
                    "temperature_c": ob[7], "humidity": ob[8], "illuminance": ob[9],
                    "uv": ob[10], "solar_wm2": ob[11], "rain_mm": ob[12],
                    "precip_type": ob[13], "lightning_km": ob[14],
                    "lightning_count": ob[15], "battery_v": ob[16],
                }
                if len(ob) > 18:
                    values["daily_rain_mm"] = ob[18]
        elif kind == "evt_strike" and len(packet.get("evt", ())) >= 2:
            values = {"last_lightning": packet["evt"][0], "lightning_km": packet["evt"][1]}
        elif kind == "evt_precip":
            values = {"precip_active": True, "last_precip": packet.get("evt", [now])[0]}
        if values:
            with self._lock:
                self._data["weather"].update(values)
                self._data["weatherflow"].update(connected=True, updated=now, error="")

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
            client.subscribe([(topic, 0) for topic in HOUSE_TOPICS])

    def _mqtt_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None):
        with self._lock:
            self._data["mqtt"].update(connected=False, error=f"DISCONNECTED {reason_code}")

    def _mqtt_message(self, _client, _userdata, message):
        now = time.time()
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        topic = message.topic
        updates = {}
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
            updates = {"house": {key: {"temperature_f": temperature, "humidity": humidity,
                                       "updated": max(stamp, humidity_stamp)}}}
        elif topic in ("smartthings/L garage door", "smartthings/R garage door"):
            contact, stamp, _ = _capability(payload, "contact")
            key = "garage_left" if "/L garage" in topic else "garage_right"
            updates = {"house": {key: {"state": contact, "updated": stamp}}}
        elif "Leak" in topic or topic.endswith("Ice Maker"):
            water, stamp, _ = _capability(payload, "water")
            updates = {"leak": (topic.split("/", 1)[-1], water, stamp)}
        with self._lock:
            if "forecast" in updates:
                self._data["forecast"].update(updates["forecast"])
            if "house" in updates:
                self._data["house"].update(updates["house"])
            if "leak" in updates:
                name, state, stamp = updates["leak"]
                self._data["house"].setdefault("leaks", {})[name] = {
                    "state": state, "updated": stamp}
            self._data["mqtt"].update(connected=True, updated=now, error="")
