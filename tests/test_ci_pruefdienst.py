"""Fail-closed central CI reporting and evidence recovery boundaries."""

import hashlib
import io
import json
import subprocess
import zipfile
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


def artifact_report(tmp_path, *, raw=None, digest=None):
    if raw is None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("fixture.xml", "external-only-fixture-payload")
        raw = buffer.getvalue()
    archive = tmp_path / "downloaded.zip"
    archive.write_bytes(raw)
    artifact = {
        "name": "rclone-ci-evidence.zip",
        "path": "reports/rclone-ci-evidence.zip",
        "encoding": "binary",
        "sha256": digest or hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "files": 1,
    }
    path = tmp_path / "quality.json"
    path.write_text(
        json.dumps({"artifacts": [artifact], "evidenceFiles": ["fixture.xml"]}),
        encoding="utf-8",
    )
    return path, archive


def test_evidence_keeps_payload_external_and_download_validation_is_read_only(tmp_path):
    plan = verify.Plan(tmp_path)
    (plan.directory / "fixture.xml").write_text(
        '<testsuite><testcase name="external-only-fixture-payload"/></testsuite>',
        encoding="utf-8",
    )
    assert verify.evidence(plan)["files"] == 1
    plan.save()
    report = tmp_path / verify.REPORT
    archive = tmp_path / "reports/rclone-ci-evidence.zip"
    before_report, before_archive = report.read_bytes(), archive.read_bytes()
    result = recovery.validate(report, archive)
    assert result["files"] == 1 and result["bytes"] == len(before_archive)
    assert archive.read_bytes() == before_archive
    assert report.read_bytes() == before_report
    artifact = plan.report["artifacts"][0]
    assert set(artifact) == {"name", "path", "encoding", "sha256", "bytes", "files"}
    assert artifact["encoding"] == "binary"
    assert b"external-only-fixture-payload" not in before_report
    assert "content" not in artifact
    with pytest.raises(FileExistsError):
        verify.evidence(plan)


def test_download_validation_rejects_non_zip_payload_with_matching_digest(tmp_path):
    path, archive = artifact_report(tmp_path, raw=b"not an archive")
    with pytest.raises(ValueError, match="not a ZIP archive"):
        recovery.validate(path, archive)


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "encoding",
        "duplicate",
        "empty",
        "bytes",
        "files",
        "payload",
        "unsafe-manifest",
        "wrong-manifest",
    ],
)
def test_download_validation_rejects_corrupt_or_ambiguous_evidence(tmp_path, change):
    path, archive = artifact_report(
        tmp_path,
        digest="0" * 64 if change == "digest" else None,
        raw=b"" if change == "empty" else None,
    )
    data = json.loads(path.read_text())
    if change == "encoding":
        data["artifacts"][0]["encoding"] = "base64"
    if change == "duplicate":
        data["artifacts"].append(dict(data["artifacts"][0]))
    if change == "bytes":
        data["artifacts"][0]["bytes"] += 1
    if change == "files":
        data["artifacts"][0]["files"] += 1
    if change == "payload":
        data["artifacts"][0]["content"] = "inline-payload-forbidden"
    if change == "unsafe-manifest":
        data["evidenceFiles"] = ["../outside.xml"]
    if change == "wrong-manifest":
        data["evidenceFiles"] = ["missing.xml"]
    path.write_text(json.dumps(data))
    original = archive.read_bytes()
    with pytest.raises(ValueError):
        recovery.validate(path, archive)
    assert archive.read_bytes() == original


def test_download_validation_enforces_64_mib_metadata_limit_before_reading_archive(
    tmp_path,
):
    assert recovery.MAX_ARCHIVE_BYTES == 64 * 1024 * 1024
    path, archive = artifact_report(tmp_path)
    data = json.loads(path.read_text())
    data["artifacts"][0]["bytes"] = 64 * 1024 * 1024 + 1
    path.write_text(json.dumps(data))
    archive.unlink()
    with pytest.raises(ValueError, match="Invalid external"):
        recovery.validate(path, archive)


def test_download_validation_accepts_exact_size_boundary_and_rejects_one_byte_over(
    tmp_path, monkeypatch
):
    path, archive = artifact_report(tmp_path)
    monkeypatch.setattr(recovery, "MAX_ARCHIVE_BYTES", archive.stat().st_size)
    assert recovery.validate(path, archive)["bytes"] == archive.stat().st_size
    monkeypatch.setattr(recovery, "MAX_ARCHIVE_BYTES", archive.stat().st_size - 1)
    with pytest.raises(ValueError, match="Invalid external"):
        recovery.validate(path, archive)


def test_evidence_builder_refuses_oversized_archive_without_publishable_metadata(
    tmp_path, monkeypatch
):
    assert verify.MAX_ARCHIVE_BYTES == 64 * 1024 * 1024
    plan = verify.Plan(tmp_path)
    (plan.directory / "fixture.xml").write_text("fixture")
    monkeypatch.setattr(verify, "MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(ValueError, match="oversized"):
        verify.evidence(plan)
    assert "artifacts" not in plan.report
