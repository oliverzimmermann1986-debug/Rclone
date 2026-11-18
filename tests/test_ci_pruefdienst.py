"""Fail-closed central CI reporting and evidence recovery boundaries."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import evidence as recovery
from scripts.ci import verify


def junit(tmp_path, cases):
    path = tmp_path / "junit.xml"
    path.write_text("<testsuite>" + "".join(cases) + "</testsuite>", encoding="utf-8")
    return path


def test_junit_records_failures_errors_and_skip_reasons(tmp_path):
    path = junit(
        tmp_path,
        [
            '<testcase classname="tests.example" name="pass"/>',
            '<testcase classname="tests.example" name="failure"><failure/></testcase>',
            '<testcase classname="tests.example" name="error"><error/></testcase>',
            '<testcase classname="tests.example" name="skip"><skipped message="missing tool"/></testcase>',
        ],
    )
    summary = verify.junit_summary(path)
    assert summary == {
        "total": 4,
        "passed": 1,
        "failed": ["tests.example.failure", "tests.example.error"],
        "skipped": [{"name": "tests.example.skip", "reason": "missing tool"}],
    }


@pytest.mark.parametrize(
    "cases,options",
    [
        ([], {}),
        (['<testcase name="only-one"/>'], {"expected": 2}),
        (['<testcase name="tiny-suite"/>'], {"minimum": 1704}),
    ],
)
def test_empty_or_truncated_python_suite_is_not_accepted(tmp_path, cases, options):
    with pytest.raises(ValueError, match="Missing/incomplete"):
        verify.junit_summary(junit(tmp_path, cases), **options)


def tap(**overrides):
    values = {
        "tests": 2,
        "pass": 2,
        "fail": 0,
        "cancelled": 0,
        "skipped": 0,
        "todo": 0,
        **overrides,
    }
    return "TAP version 13\n" + "\n".join(
        f"# {key} {value}" for key, value in values.items()
    )


def test_node_requires_complete_summary_and_preserves_real_counts():
    assert verify.node_summary(tap())["pass"] == 2


@pytest.mark.parametrize(
    "output",
    [
        "",
        tap(tests=0, **{"pass": 0}),
        tap(fail=1),
        tap(skipped=1),
        tap(cancelled=1),
        tap(todo=1),
        tap(**{"pass": 1}),
        tap() + "\n# tests 2",
    ],
)
def test_incomplete_node_results_fail(output):
    with pytest.raises(ValueError):
        verify.node_summary(output)


def visual_report():
    return {
        "passed": True,
        "syntheticOnly": True,
        "cases": [{"name": f"case-{index}", "passed": True} for index in range(48)],
        "unexpectedRequests": [],
        "blockedExternalRequests": [],
        "playwright": "1.65.1",
        "chromium": "159.0.8154.6",
    }


def test_visual_summary_accepts_all_48_unique_synthetic_cases(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(visual_report()), encoding="utf-8")
    assert verify.visual_summary(path)["passed"] == 48


@pytest.mark.parametrize(
    "change",
    [
        {"passed": False},
        {"syntheticOnly": False},
        {"cases": [{"name": "repeated", "passed": True}] * 48},
        {"cases": [{"name": "incomplete", "passed": True}]},
        {"unexpectedRequests": [{"path": "/unmocked"}]},
        {"blockedExternalRequests": ["https://account.example.invalid"]},
        {"chromium": "different-version"},
        {"playwright": "different-version"},
    ],
)
def test_visual_report_cannot_hide_missing_cases_or_unexpected_requests(
    tmp_path, change
):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({**visual_report(), **change}), encoding="utf-8")
    with pytest.raises(ValueError, match="48 passing synthetic"):
        verify.visual_summary(path)


def test_plan_saves_all_gates_before_execution_and_failed_prerequisites_block_work(
    tmp_path,
):
    plan = verify.Plan(tmp_path)
    saved = json.loads((tmp_path / verify.REPORT).read_text(encoding="utf-8"))
    assert tuple(gate["id"] for gate in saved["gates"]) == verify.GATES
    assert all(gate["status"] == "not_run" for gate in saved["gates"])
    invoked = []

    def fail():
        raise RuntimeError("fixture failure")

    plan.gate("runtime", fail)
    plan.gate(
        "python-dependencies-3.10", lambda: invoked.append("blocked"), ("runtime",)
    )
    plan.gate("installer", lambda: {"independent": True})
    saved = json.loads((tmp_path / verify.REPORT).read_text(encoding="utf-8"))
    assert plan.item("runtime")["status"] == "failed"
    assert plan.item("python-dependencies-3.10")["status"] == "not_run"
    assert plan.item("installer")["status"] == "passed"
    assert invoked == []
    assert len(saved["gates"]) == len(verify.GATES)


def test_action_metadata_cannot_override_failed_or_missing_outcomes(tmp_path):
    plan = verify.Plan(tmp_path)
    plan.gate("runtime", lambda: {"status": "passed", "id": "fake"})
    assert plan.item("runtime")["status"] == "failed"
    assert plan.item("runtime")["id"] == "runtime"


def test_controller_gate_requires_staged_test_files(tmp_path, monkeypatch):
    plan = verify.Plan(tmp_path)
    monkeypatch.setattr(plan, "command", lambda *_args, **_kwargs: "")
    plan.gate("central-controller", lambda: verify.controller_tests(plan))
    assert plan.item("central-controller")["status"] == "failed"
    assert "unit suites are missing" in plan.item("central-controller")["reason"]


def test_child_environment_removes_credentials_and_uses_private_empty_config(
    tmp_path, monkeypatch
):
    for key in (
        "GH_TOKEN",
        "CM_API_TOKEN",
        "ASC_PRIVATE_KEY",
        "RCLONE_CONFIG_DRIVE_TOKEN",
        "IOS_P12_PASSWORD_RCLONE",
    ):
        monkeypatch.setenv(key, "fixture-private-value")
    plan = verify.Plan(tmp_path)
    assert "fixture-private-value" not in plan.env.values()
    assert Path(plan.env["RCLONE_CONFIG"]).is_relative_to(plan.work)
    assert plan.env["RCLONE_ASK_PASSWORD"] == "false"


def test_commands_do_not_reuse_output_from_an_earlier_passing_suite(
    tmp_path, monkeypatch
):
    plan = verify.Plan(tmp_path)
    values = iter(["passing marker\n", "new command without marker\n"])

    class Process:
        returncode = 0

        def __init__(self, *args, **kwargs):
            kwargs["stdout"].write(next(values).encode())

        def communicate(self, **kwargs):
            return None, None

    monkeypatch.setattr(verify.subprocess, "Popen", Process)
    assert "passing marker" in plan.command("web-behavior", ["fixture"])
    assert "passing marker" not in plan.command("web-behavior", ["fixture"])
    assert "passing marker" in (plan.directory / "web-behavior.log").read_text()


def test_timed_out_command_terminates_its_process_group_and_fails(
    tmp_path, monkeypatch
):
    plan = verify.Plan(tmp_path)
    killed = []
    waited = []

    class Process:
        pid = 424242

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, **kwargs):
            raise subprocess.TimeoutExpired("fixture", kwargs["timeout"])

        def wait(self):
            waited.append(True)

    monkeypatch.setattr(verify.subprocess, "Popen", Process)
    monkeypatch.setattr(verify.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(RuntimeError, match="timed out"):
        plan.command("installer", ["fixture"], timeout=1)
    assert killed == [(424242, verify.signal.SIGKILL)]
    assert waited == [True]


def test_python_oauth_skip_remains_a_failed_gate_with_the_exact_reason(
    tmp_path, monkeypatch
):
    plan = verify.Plan(tmp_path)

    def fake_command(key, *args, **kwargs):
        (plan.directory / f"{key}.xml").write_text(
            '<testsuite><testcase name="one"/><testcase name="two"><skipped message="rclone missing"/></testcase></testsuite>',
            encoding="utf-8",
        )
        return ""

    monkeypatch.setattr(plan, "command", fake_command)
    plan.gate(
        "oauth-legacy", lambda: verify.python_tests(plan, "3.12", key="oauth-legacy")
    )
    assert plan.item("oauth-legacy")["status"] == "failed"
    assert plan.item("oauth-legacy")["tests"]["skipped"] == [
        {"name": ".two", "reason": "rclone missing"}
    ]


def test_passing_visual_gate_requires_preserved_screenshots_and_traces(tmp_path):
    plan = verify.Plan(tmp_path)
    plan.item("web-visual")["status"] = "passed"
    (plan.directory / "fixture.xml").write_text("<testsuite/>")
    with pytest.raises(ValueError, match="48 screenshots"):
        verify.evidence(plan)


def artifact_report(tmp_path, *, raw=b"fixture zip bytes", digest=None):
    artifact = {
        "name": "rclone-ci-evidence.zip",
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
        "sha256": digest or hashlib.sha256(raw).hexdigest(),
    }
    path = tmp_path / "quality.json"
    path.write_text(json.dumps({"artifacts": [artifact]}), encoding="utf-8")
    return path


def test_evidence_round_trip_checks_integrity_and_never_overwrites(tmp_path):
    plan = verify.Plan(tmp_path)
    (plan.directory / "fixture.xml").write_text(
        '<testsuite><testcase name="fixture"/></testsuite>', encoding="utf-8"
    )
    assert verify.evidence(plan)["files"] == 1
    plan.save()
    target = tmp_path / "output.zip"
    recovery.recover(tmp_path / verify.REPORT, target)
    assert target.read_bytes().startswith(b"PK")
    with pytest.raises(FileExistsError):
        recovery.recover(tmp_path / verify.REPORT, target)


def test_recovery_rejects_non_zip_payload_even_with_a_matching_digest(tmp_path):
    path = artifact_report(tmp_path)
    with pytest.raises(ValueError, match="not a ZIP archive"):
        recovery.recover(path, tmp_path / "output.zip")
    assert not (tmp_path / "output.zip").exists()


@pytest.mark.parametrize("change", ["digest", "encoding", "duplicate", "empty"])
def test_evidence_recovery_rejects_corruption_or_ambiguous_archives(tmp_path, change):
    path = artifact_report(
        tmp_path,
        digest="0" * 64 if change == "digest" else None,
        raw=b"" if change == "empty" else b"fixture",
    )
    data = json.loads(path.read_text())
    if change == "encoding":
        data["artifacts"][0]["content"] = "!invalid!"
    if change == "duplicate":
        data["artifacts"].append(dict(data["artifacts"][0]))
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        recovery.recover(path, tmp_path / "output.zip")
    assert not (tmp_path / "output.zip").exists()
