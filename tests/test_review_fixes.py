"""Regressionen aus der Abschlussreview: Doctor-Route, Airgap-Konflikt, Filterreihenfolge."""

from app.jobs import pair_planner
from app.jobs.rclone_sync import _build_pair_command, _protected_filter_args
from app.routes import api_diagnostics
from app.routes.api_maintenance import minimize_job_summaries


def test_doctor_route_points_at_doctor_handler():
    routes = {route.path: route.endpoint for route in api_diagnostics.router.routes}
    assert routes["/api/diagnostics/doctor"] is api_diagnostics.doctor


def test_pairs_on_same_airgap_device_conflict():
    first = {"local": "/media/usb/a", "remote": "r:/a", "airgap_device": "/dev/sdb1"}
    second = {"local": "/media/usb/b", "remote": "r:/b", "airgap_device": "/dev/sdb1"}
    assert pair_planner.pairs_conflict(first, second)
    assert not pair_planner.pairs_conflict(first, {**second, "airgap_device": ""})


def test_user_filter_group_order_keeps_excludes_ahead_of_includes():
    # Bestehende Semantik (tests/test_reserved_paths.py): Excludes vor Includes.
    rules = _protected_filter_args(
        ["--include", "Fotos/**", "--exclude", "*.tmp", "--filter", "- cache/**"],
        leading_rules=["- /tmp/x.txt"],
    )
    filters = [rules[i + 1] for i, token in enumerate(rules) if token == "--filter"]
    assert filters == [
        "- /Sicherpfad/**",
        "- /.sicherpfad-snapshots/**",
        "- /.rclone-versions/**",
        "- /tmp/x.txt",
        "- *.tmp",
        "- cache/**",
        "+ Fotos/**",
        "- **",
    ]


def test_build_pair_command_puts_excludes_right_after_protection():
    pair = {
        "name": "Fotos",
        "remote": "r:/Fotos",
        "local": "/data/Fotos",
        "direction": "push",
        "mode": "copy",
        "include": ["keep/**"],
    }
    cmd, *_ = _build_pair_command(
        pair, [], True, config_snapshot={"backup": {}}, exclude_paths=["a.txt"]
    )
    filters = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--filter"]
    assert filters.index("- /a.txt") == 3
    assert filters.index("- /a.txt") < filters.index("+ keep/**")


def test_support_bundle_replaces_excluded_paths_with_count():
    out = minimize_job_summaries([{"summary": {"excluded_paths": ["a", "b"]}}])
    assert out[0]["summary"] == {"excluded_paths_count": 2}
