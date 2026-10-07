#!/usr/bin/env python3
"""List SmartThings devices and capabilities without printing the API token."""

import json
import os
import sys

from office_telemetry import SMARTTHINGS_CONFIG, _smartthings_api_json


def main():
    path = os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 else SMARTTHINGS_CONFIG
    try:
        with open(path, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read {path}: {exc}")
    token = str(config.get("token") or config.get("access_token") or "").strip()
    if not token:
        raise SystemExit(f"No SmartThings token is configured in {path}")
    query = {"locationId": config["location_id"]} if config.get("location_id") else None
    payload = _smartthings_api_json("/devices", token, query)
    rows = []
    for device in payload.get("items", []):
        capabilities = set()
        for component in device.get("components", []):
            for capability in component.get("capabilities", []):
                if capability.get("id"):
                    capabilities.add(capability["id"])
        rows.append({
            "label": device.get("label") or device.get("name"),
            "device_id": device.get("deviceId"),
            "type": device.get("type"),
            "capabilities": sorted(capabilities),
        })
    print(json.dumps(sorted(rows, key=lambda row: str(row["label"]).casefold()), indent=2))
    print(f"\n{len(rows)} devices; authentication token was not printed.", file=sys.stderr)


if __name__ == "__main__":
    main()
