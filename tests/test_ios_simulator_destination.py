import json

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
    assert capsys.readouterr().out.strip() == "created"


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
