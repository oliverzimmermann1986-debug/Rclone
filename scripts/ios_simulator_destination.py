"""Select (or create) an available iPhone instead of assuming an image model."""

from __future__ import annotations

import json
import re
import subprocess
import sys


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


def diagnostic_inventory(inventory):
    """Keep diagnostics limited to simulator metadata, never the environment."""
    runtime_keys = ("identifier", "name", "version", "buildversion", "isAvailable")
    device_keys = ("udid", "name", "state", "isAvailable")
    return {
        "runtimes": [
            {key: runtime[key] for key in runtime_keys if key in runtime}
            for runtime in inventory.get("runtimes", [])
        ],
        "devices": {
            runtime_id: [
                {key: device[key] for key in device_keys if key in device}
                for device in devices
            ]
            for runtime_id, devices in inventory.get("devices", {}).items()
        },
    }


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
            "name": "Sicherpfad CI",
            "udid": command(
                "create",
                "Sicherpfad CI",
                device_type["identifier"],
                runtime["identifier"],
            ),
        }
    else:
        runtime_id = next(
            runtime_id
            for runtime_id, devices in inventory.get("devices", {}).items()
            if any(item.get("udid") == device["udid"] for item in devices)
        )
        runtime = next(
            item for item in inventory["runtimes"] if item["identifier"] == runtime_id
        )
    selected = {
        "udid": device["udid"],
        "name": device.get("name"),
        "state": device.get("state"),
        "runtime": runtime["identifier"],
        "runtime_version": runtime["version"],
    }
    print(
        f"Selected iOS simulator: {json.dumps(selected)}", file=sys.stderr, flush=True
    )
    stage = "boot"
    try:
        if device.get("state") != "Booted":
            command("boot", device["udid"])
        stage = "bootstatus"
        command("bootstatus", device["udid"], "-b")
    except (subprocess.CalledProcessError, OSError) as error:
        print(f"simctl {stage} failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        try:
            inventory = json.loads(command("list", "--json"))
        except (subprocess.CalledProcessError, OSError, ValueError) as diagnostic_error:
            print(
                f"Could not refresh simulator inventory; using initial inventory: {diagnostic_error}",
                file=sys.stderr,
            )
        print(
            f"Simulator inventory: {json.dumps(diagnostic_inventory(inventory))}",
            file=sys.stderr,
            flush=True,
        )
        raise
    print(device["udid"])


if __name__ == "__main__":
    main()
