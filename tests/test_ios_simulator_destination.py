import json
import subprocess

import pytest

from scripts import ios_simulator_destination as simulator


def inventory():
    return {
        "runtimes": [
            {"identifier": "runtime.iOS-26-0", "version": "26.0", "isAvailable": True},
            {"identifier": "runtime.iOS-26-6", "version": "26.6", "isAvailable": True},
        ],
        "devicetypes": [{"name": "iPhone 17", "identifier": "device.iphone17"}],
        "devices": {
            "runtime.iOS-26-0": [
                {"name": "iPhone 17", "udid": "old", "isAvailable": True}
            ],
            "runtime.iOS-26-6": [
                {"name": "iPhone 17", "udid": "unavailable", "isAvailable": False},
                {"name": "iPhone 16", "udid": "latest", "isAvailable": True},
            ],
        },
    }


def test_available_runtime_wins_over_assumed_iphone_model():
    assert simulator.select_iphone(inventory())["udid"] == "latest"


def test_empty_device_inventory_creates_and_boots_an_iphone(monkeypatch, capsys):
    data = inventory()
    data["devices"] = {}
    calls = []

    def command(*args):
        calls.append(args)
        if args[0] == "list":
            return json.dumps(data)
        return "created" if args[0] == "create" else ""

    monkeypatch.setattr(simulator, "command", command)
    simulator.main()
    assert calls == [
        ("list", "--json"),
        ("create", "Sicherpfad CI", "device.iphone17", "runtime.iOS-26-6"),
        ("boot", "created"),
        ("bootstatus", "created", "-b"),
    ]
    captured = capsys.readouterr()
    assert captured.out == "created\n"
    selected = json.loads(captured.err.removeprefix("Selected iOS simulator: "))
    assert selected["name"] == "Sicherpfad CI"
    assert selected["runtime"] == "runtime.iOS-26-6"


def test_missing_available_runtime_fails_before_creating_a_device(monkeypatch):
    data = inventory()
    data["runtimes"] = []
    calls = []

    def command(*args):
        calls.append(args)
        return json.dumps(data)

    monkeypatch.setattr(simulator, "command", command)
    with pytest.raises(SystemExit, match="No available iOS runtime"):
        simulator.main()
    assert calls == [("list", "--json")]


def test_existing_device_logs_metadata_only_to_stderr(monkeypatch, capsys):
    calls = []

    def command(*args):
        calls.append(args)
        return json.dumps(inventory()) if args[0] == "list" else "boot output"

    monkeypatch.setattr(simulator, "command", command)
    simulator.main()

    captured = capsys.readouterr()
    assert captured.out == "latest\n"
    selected = json.loads(captured.err.removeprefix("Selected iOS simulator: "))
    assert selected == {
        "udid": "latest",
        "name": "iPhone 16",
        "state": None,
        "runtime": "runtime.iOS-26-6",
        "runtime_version": "26.6",
    }
    assert calls == [
        ("list", "--json"),
        ("boot", "latest"),
        ("bootstatus", "latest", "-b"),
    ]


@pytest.mark.parametrize("failed_stage", ["boot", "bootstatus"])
def test_boot_failure_preserves_error_and_logs_current_inventory(
    monkeypatch, capsys, failed_stage
):
    data = inventory()
    calls = []
    original = subprocess.CalledProcessError(
        70, ["xcrun", "simctl", failed_stage, "latest"], stderr="simulator unavailable"
    )

    def command(*args):
        calls.append(args)
        if args[0] == "list":
            return json.dumps(data)
        if args[0] == failed_stage:
            data["devices"]["runtime.iOS-26-6"][1]["state"] = "Shutdown"
            data["credentials"] = "not-simulator-metadata"
            raise original
        return ""

    monkeypatch.setattr(simulator, "command", command)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        simulator.main()

    assert raised.value is original
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"simctl {failed_stage} failed" in captured.err
    assert "simulator unavailable" in captured.err
    assert '"state": "Shutdown"' in captured.err
    assert "not-simulator-metadata" not in captured.err
    assert calls.count(("list", "--json")) == 2
    assert sum(call[0] == failed_stage for call in calls) == 1


def test_failed_inventory_diagnostic_does_not_mask_boot_failure(monkeypatch, capsys):
    original = subprocess.CalledProcessError(70, ["xcrun", "simctl", "boot", "latest"])
    list_calls = 0

    def command(*args):
        nonlocal list_calls
        if args[0] == "list":
            list_calls += 1
            if list_calls > 1:
                raise OSError("inventory unavailable")
            return json.dumps(inventory())
        raise original

    monkeypatch.setattr(simulator, "command", command)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        simulator.main()

    assert raised.value is original
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "using initial inventory" in captured.err
    assert "runtime.iOS-26-6" in captured.err
