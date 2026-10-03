"""Select (or create) an available iPhone instead of assuming an image model."""

from __future__ import annotations

import json
import re
import subprocess


def select_iphone(inventory):
    runtimes = {
        runtime["identifier"]: runtime
        for runtime in inventory.get("runtimes", [])
        if runtime.get("isAvailable") and ".iOS-" in runtime["identifier"]
    }
    candidates = []
    for runtime_id, devices in inventory.get("devices", {}).items():
        if runtime_id not in runtimes:
            continue
        version = tuple(
            int(part) for part in re.findall(r"\d+", runtimes[runtime_id]["version"])
        )
        for device in devices:
            if device.get("isAvailable") and device.get("name", "").startswith(
                "iPhone"
            ):
                candidates.append((version, device["name"] == "iPhone 17", device))
    if candidates:
        return max(candidates, key=lambda value: value[:2])[2]
    return None


def command(*args):
    return subprocess.run(
        ["xcrun", "simctl", *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def main():
    inventory = json.loads(command("list", "--json"))
    device = select_iphone(inventory)
    if device is None:
        runtimes = [
            r
            for r in inventory.get("runtimes", [])
            if r.get("isAvailable") and ".iOS-" in r["identifier"]
        ]
        types = [
            d
            for d in inventory.get("devicetypes", [])
            if d.get("name", "").startswith("iPhone")
        ]
        if not runtimes or not types:
            raise SystemExit(
                "No available iOS runtime/iPhone device type on this runner"
            )
        runtime = max(
            runtimes, key=lambda r: tuple(int(x) for x in r["version"].split("."))
        )
        device_type = next((d for d in types if d["name"] == "iPhone 17"), types[-1])
        device = {
            "udid": command(
                "create",
                "Sicherpfad CI",
                device_type["identifier"],
                runtime["identifier"],
            )
        }
    if device.get("state") != "Booted":
        command("boot", device["udid"])
    command("bootstatus", device["udid"], "-b")
    print(device["udid"])


if __name__ == "__main__":
    main()
