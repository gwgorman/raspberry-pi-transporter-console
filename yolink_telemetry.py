"""Read-only YoLink cloud telemetry for the transporter Environmental page.

Credentials belong in ~/.config/startrek-yolink.json (mode 0600), never here.
The adapter uses HTTPS for discovery/state and YoLink's documented TCP MQTT
port for reports.  It never publishes or controls a device.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


DEFAULT_CONFIG = os.path.expanduser("~/.config/startrek-yolink.json")
TOKEN_URL = "https://api.yosmart.com/open/yolink/token"
API_URL = "https://api.yosmart.com/open/yolink/v2/api"
MQTT_HOST = "mqtt.api.yosmart.com"
MQTT_PORT = 8003
SUPPORTED_TYPES = ("THSensor", "DoorSensor")


def _epoch(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


class YoLinkService:
    """One failure-isolated worker with a thread-safe display snapshot."""

    def __init__(self, config_path=DEFAULT_CONFIG):
        self.config_path = config_path
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._mqtt = None
        self._config = {}
        self._inventory = {}
        self._devices = {}
        self._history = None
        self._data = {
            "source": {"state": "NOT CONFIGURED", "last_activity": 0.0, "error": ""},
            "temperature_sensors": [],
            "shed": {"state": "UNKNOWN", "online": None},
        }

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="yolink-environment", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        client = self._mqtt
        if client:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:
                pass
        if self._history:
            try:
                self._history.close()
            except sqlite3.Error:
                pass

    def snapshot(self):
        with self._lock:
            self._refresh_snapshot()
            return copy.deepcopy(self._data)

    def _set_source(self, state, error=""):
        with self._lock:
            self._data["source"]["state"] = state
            # Errors are deliberately concise so secrets and response bodies never reach UI/logs.
            self._data["source"]["error"] = str(error)[:120]

    def _load_config(self):
        try:
            if os.stat(self.config_path).st_mode & 0o077:
                self._set_source("AUTH REQUIRED", "CONFIG FILE MUST USE MODE 0600")
                return None
            with open(self.config_path, encoding="utf-8") as stream:
                config = json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self._set_source("AUTH REQUIRED", f"CONFIGURATION ERROR: {type(exc).__name__}")
            return None
        if not config.get("enabled", False):
            return None
        if not config.get("client_id") or not config.get("client_secret"):
            self._set_source("AUTH REQUIRED", "CREDENTIALS INCOMPLETE")
            return None
        return config

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            self._config = self._load_config() or {}
            if not self._config:
                if self._data["source"]["state"] != "AUTH REQUIRED":
                    self._set_source("NOT CONFIGURED")
                self._stop.wait(10)
                continue
            try:
                self._open_history()
                self._set_source("RECONNECTING")
                token, expires = self._get_token()
                home_id = self._config.get("home_id") or self._api(token, "Home.getGeneralInfo").get("id")
                devices = self._api(token, "Home.getDeviceList").get("devices", [])
                self._inventory = {item.get("deviceId"): item for item in devices if item.get("deviceId")}
                self._connect_mqtt(token, home_id)
                # Subscribe is established before the initial state sweep to avoid a startup gap.
                deadline = time.monotonic() + 8
                while not self._stop.is_set() and self._data["source"]["state"] != "CONNECTED" and time.monotonic() < deadline:
                    self._stop.wait(.1)
                self._initial_sweep(token)
                attempt = 0
                refresh_after = max(300, min(float(expires or 3600) * .80, 43200))
                self._stop.wait(refresh_after)
            except Exception as exc:
                self._set_source("AUTH REQUIRED" if isinstance(exc, PermissionError) else "RECONNECTING",
                                 type(exc).__name__)
                attempt += 1
                self._stop.wait(min(300, (2 ** min(attempt, 7)) + random.random() * 3))
            finally:
                self._disconnect_mqtt()

    def _get_token(self):
        payload = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._config["client_id"],
            "client_secret": self._config["client_secret"],
        }).encode()
        request = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError("YoLink credentials rejected") from None
            raise
        token = result.get("access_token")
        if not token:
            raise PermissionError("YoLink token unavailable")
        return token, result.get("expires_in", 3600)

    @staticmethod
    def _api(token, method, device=None):
        now = int(time.time() * 1000)
        body = {"method": method, "time": now, "msgid": str(now)}
        if device:
            body.update(targetDevice=device["deviceId"], token=device["token"], params={})
        request = urllib.request.Request(
            API_URL, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.load(response)
        if result.get("code") != "000000":
            raise RuntimeError(f"YoLink API {method} failed with code {result.get('code', 'UNKNOWN')}")
        return result.get("data") or {}

    def _connect_mqtt(self, token, home_id):
        if mqtt is None:
            raise RuntimeError("PAHO MQTT NOT INSTALLED")
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=f"startrek-{uuid.uuid4().hex[:12]}")
        except AttributeError:
            client = mqtt.Client(client_id=f"startrek-{uuid.uuid4().hex[:12]}")
        client.username_pw_set(token, None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.user_data_set({"topic": f"yl-home/{home_id}/+/report"})
        client.reconnect_delay_set(min_delay=2, max_delay=120)
        client.connect(MQTT_HOST, MQTT_PORT, 60)
        client.loop_start()
        self._mqtt = client

    def _disconnect_mqtt(self):
        client, self._mqtt = self._mqtt, None
        if client:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:
                pass

    def _on_connect(self, client, userdata, _flags, reason_code, _properties=None):
        connected = (not reason_code.is_failure) if hasattr(reason_code, "is_failure") else reason_code == 0
        if connected:
            client.subscribe(userdata["topic"], qos=0)
            self._set_source("CONNECTED")
        else:
            self._set_source("RECONNECTING", "MQTT CONNECTION REFUSED")

    def _on_disconnect(self, _client, _userdata, *callback_args):
        # Paho v1 passes only rc; v2 passes disconnect flags, reason code, properties.
        reason_code = callback_args[-2] if len(callback_args) >= 2 else callback_args[-1] if callback_args else 0
        if not self._stop.is_set() and reason_code:
            self._set_source("RECONNECTING", "MQTT DISCONNECTED")

    def _on_message(self, _client, _userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        self._ingest(payload)

    def _initial_sweep(self, token):
        for device in self._inventory.values():
            if self._stop.is_set() or device.get("type") not in SUPPORTED_TYPES:
                continue
            try:
                data = self._api(token, f"{device['type']}.getState", device)
                data.setdefault("deviceId", device["deviceId"])
                self._ingest({"method": f"{device['type']}.getState", "data": data})
            except Exception:
                # One unavailable sensor must not take down the source or kiosk.
                continue

    def _ingest(self, packet):
        data = packet.get("data") if isinstance(packet, dict) else None
        if not isinstance(data, dict):
            return
        device_id = data.get("deviceId") or packet.get("deviceId")
        inventory = self._inventory.get(device_id, {})
        device_type = inventory.get("type") or str(packet.get("method", "")).split(".")[0]
        if device_type not in SUPPORTED_TYPES or not device_id:
            return
        state = data.get("state") if isinstance(data.get("state"), dict) else data
        reported = _epoch(data.get("reportAt") or packet.get("time")) or time.time()
        normalized = {
            "id": device_id,
            "name": self._display_name(inventory),
            "type": device_type,
            "model": inventory.get("modelName", ""),
            "online": data.get("online"),
            "battery": state.get("battery"),
            "reported_at": reported,
            "received_at": time.time(),
        }
        if device_type == "THSensor":
            temperature = state.get("temperature")
            # YoLink THSensor API temperatures are Celsius.  Some YS8017 units
            # report mode="F" to describe the device/app display preference,
            # but the numeric API value remains Celsius.
            unit = "C"
            normalized.update(
                temperature_raw=temperature,
                temperature_unit=unit,
                temperature_f=(float(temperature) * 9 / 5 + 32
                               if temperature is not None else None),
                humidity=float(state["humidity"]) if state.get("humidity") is not None else None,
                alarm=state.get("state") == "alert" or any(bool(value) for value in (state.get("alarm") or {}).values()),
            )
        else:
            normalized.update(state=str(state.get("state", "unknown")).lower(),
                              changed_at=_epoch(state.get("stateChangedAt")))
        with self._lock:
            previous = self._devices.get(device_id)
            if previous and previous.get("reported_at", 0) > reported:
                return
            self._devices[device_id] = normalized
            self._data["source"]["last_activity"] = time.time()
            self._refresh_snapshot()
        self._record(normalized, previous)

    def _display_name(self, inventory):
        device_id = inventory.get("deviceId", "")
        overrides = self._config.get("name_overrides", {})
        return overrides.get(device_id) or inventory.get("name") or inventory.get("modelName") or "UNNAMED SENSOR"

    def _refresh_snapshot(self):
        order = self._config.get("sensor_order", [])
        rank = {device_id: index for index, device_id in enumerate(order)}
        temperatures = [copy.deepcopy(item) for item in self._devices.values() if item.get("type") == "THSensor"]
        temperatures.sort(key=lambda item: (rank.get(item["id"], 9999), item.get("name", "").lower()))
        shed_id = self._config.get("shed_device_id")
        shed = self._devices.get(shed_id) if shed_id else None
        if not shed:
            candidates = [item for item in self._devices.values()
                          if item.get("type") == "DoorSensor" and "shed" in item.get("name", "").lower()]
            shed = candidates[0] if len(candidates) == 1 else None
        self._data["temperature_sensors"] = temperatures
        self._data["shed"] = copy.deepcopy(shed) if shed else {"state": "UNKNOWN", "online": None}

    def _open_history(self):
        if self._history:
            return
        path = os.path.expanduser(self._config.get(
            "history_path", "~/.local/share/startrek-console/yolink-history.db"))
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            self._history = sqlite3.connect(path, check_same_thread=False)
            self._history.execute("CREATE TABLE IF NOT EXISTS observations "
                                  "(device_id TEXT, observed REAL, kind TEXT, value TEXT, "
                                  "PRIMARY KEY(device_id, observed, kind))")
            cutoff = time.time() - float(self._config.get("history_days", 7)) * 86400
            self._history.execute("DELETE FROM observations WHERE observed < ?", (cutoff,))
            self._history.commit()
        except sqlite3.Error:
            self._history = None

    def _record(self, device, previous):
        if not self._history:
            return
        try:
            if device["type"] == "THSensor" and device.get("temperature_f") is not None:
                value = json.dumps({"temperature_f": device["temperature_f"], "humidity": device.get("humidity")})
                kind = "temperature"
            elif device["type"] == "DoorSensor" and (not previous or previous.get("state") != device.get("state")):
                value, kind = device.get("state", "unknown"), "door"
            else:
                return
            self._history.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?)",
                                  (device["id"], device["reported_at"], kind, value))
            self._history.commit()
        except sqlite3.Error:
            pass


def inventory(config_path=DEFAULT_CONFIG):
    """Return a redacted inventory for setup diagnostics; never includes tokens."""
    service = YoLinkService(config_path)
    service._config = service._load_config() or {}
    if not service._config:
        raise SystemExit("YoLink is not configured or enabled")
    token, _ = service._get_token()
    devices = service._api(token, "Home.getDeviceList").get("devices", [])
    return [{key: item.get(key) for key in ("deviceId", "name", "type", "modelName")}
            for item in devices]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Redacted YoLink inventory diagnostic")
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()
    if args.inventory:
        print(json.dumps(inventory(args.config), indent=2))
