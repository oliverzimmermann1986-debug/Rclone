import copy

import pytest

from app import operations_cli
from app.config_validation import ConfigValidationError, validate_config
from app.routes.api_config import _sensitive_config_changed
from app.secret_redaction import REDACTED, is_secret_key, redact_secrets


def base(tmp_path):
    return {
        "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
        "paths": {"data_dir": str(tmp_path)},
        "backup": {"pairs": []},
    }


def test_operational_defaults_do_not_enable_external_transmission(tmp_path):
    cfg, warnings = validate_config(base(tmp_path))
    assert cfg["notifications"]["heartbeat"]["enabled"] is False
    assert cfg["reports"]["weekly"]["enabled"] is False
    assert cfg["reports"]["weekly"]["email"]["enabled"] is False
    assert cfg["backup"]["storage_forecast"]["sample_interval_hours"] == 24
    assert not any("reports" in warning for warning in warnings)


def test_heartbeat_token_url_is_always_redacted():
    url = "https://watch.example.org/ping/private-test-canary"
    assert is_secret_key("secret_url")
    assert redact_secrets({"heartbeat": {"secret_url": url}}) == {
        "heartbeat": {"secret_url": REDACTED}
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://watch.example.org/ping",
        "https://user:pass@watch.example.org/",
        "https://watch.example.org/#secret",
        "https://watch.example.org/\r\nInjected",
    ],
)
def test_invalid_receiver_addresses_fail_without_echoing_secret(tmp_path, url):
    cfg = base(tmp_path)
    cfg["notifications"] = {"heartbeat": {"enabled": True, "secret_url": url}}
    with pytest.raises(ConfigValidationError) as error:
        validate_config(cfg)
    assert url not in str(error.value)


def test_defaults_do_not_require_reauth_but_destination_changes_do(tmp_path):
    before, _ = validate_config(base(tmp_path))
    after = copy.deepcopy(before)
    del before["notifications"]["heartbeat"]
    del before["reports"]
    assert not _sensitive_config_changed(before, after)
    after["notifications"]["heartbeat"]["secret_url"] = "https://watch.example.org/ping"
    assert _sensitive_config_changed(before, after)


@pytest.mark.parametrize(
    "email",
    [
        {"encryption": "none"},
        {"to_addresses": ["admin@example.org\r\nBcc:bad@example.org"]},
        {"host": "smtp.example.org\n"},
    ],
)
def test_report_delivery_settings_reject_insecure_or_injected_fields(tmp_path, email):
    cfg = base(tmp_path)
    cfg["reports"] = {"weekly": {"email": email}}
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_forecast_capacities_are_bounded_and_finite(tmp_path):
    cfg = base(tmp_path)
    cfg["backup"]["storage_forecast"] = {"capacities": {"path:remote": float("nan")}}
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_operational_tick_sends_heartbeat_first_and_isolates_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(operations_cli, "is_locked_down", lambda _db: False)
    monkeypatch.setattr(
        operations_cli.heartbeat, "tick", lambda *_: calls.append("heartbeat")
    )

    def sample(*_):
        calls.append("forecast")
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(operations_cli.storage_forecast, "sample_if_due", sample)
    monkeypatch.setattr(
        operations_cli.protection_reports, "tick", lambda *_: calls.append("report")
    )
    assert operations_cli.run_once({}, object()) == 1
    assert calls == ["heartbeat", "forecast", "report"]


def test_lockdown_prevents_sampling_and_report_transmission(monkeypatch):
    calls = []
    monkeypatch.setattr(operations_cli, "is_locked_down", lambda _db: True)
    monkeypatch.setattr(
        operations_cli.heartbeat, "tick", lambda *_: calls.append("guarded-heartbeat")
    )
    monkeypatch.setattr(
        operations_cli.storage_forecast,
        "sample_if_due",
        lambda *_: pytest.fail("forecast during lockdown"),
    )
    monkeypatch.setattr(
        operations_cli.protection_reports,
        "tick",
        lambda *_: pytest.fail("report during lockdown"),
    )
    assert operations_cli.run_once({}, object()) == 0
    assert calls == ["guarded-heartbeat"]
