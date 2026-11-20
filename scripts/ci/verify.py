"""Complete Linux quality plan for the isolated Sicherpfad Pruefdienst image."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path("reports/unified-quality-report.json")
PYTHONS = ("3.10", "3.11", "3.12", "3.13")
LEGACY_RCLONE = Path("/usr/bin/rclone")
PRODUCTION_RCLONE = Path("/opt/rclone-ci/rclone-1.74.4/rclone")
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 14 * 1024 * 1024
GATES = (
    "runtime",
    "central-controller",
    *(
        f"python-{kind}-{version}"
        for version in PYTHONS
        for kind in ("dependencies", "quality", "tests")
    ),
    "oauth-legacy",
    "oauth-production",
    "installer",
    "web-dependencies",
    "web-behavior",
    "web-visual",
    "evidence",
)
UNITS = (
    "rclone-sync-web.service",
    "rclone-sync.service",
    "rclone-sync.timer",
    "sync-scheduler.service",
    "sync-scheduler.timer",
)


def junit_summary(path: Path, *, expected: int | None = None, minimum: int = 1) -> dict:
    """Require real passing cases; every failure, error or skip remains visible."""
    cases = list(ET.parse(path).getroot().iter("testcase"))
    failed, skipped = [], []
    for case in cases:
        name = f"{case.get('classname', '')}.{case.get('name', '')}"
        skip = case.find("skipped")
        if skip is not None:
            skipped.append({"name": name, "reason": skip.get("message", "")})
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(name)
    if len(cases) < minimum or (expected is not None and len(cases) != expected):
        raise ValueError(
            f"Missing/incomplete JUnit suite: {len(cases)} cases; expected {expected}"
        )
    summary = {
        "total": len(cases),
        "passed": len(cases) - len(failed) - len(skipped),
        "failed": failed,
        "skipped": skipped,
    }
    return summary


def node_summary(output: str) -> dict:
    counts = {}
    for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
        matches = re.findall(rf"^# {key} (\d+)\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError(f"Missing or ambiguous Node summary: {key}")
        counts[key] = int(matches[0])
    if (
        not counts["tests"]
        or counts["pass"] != counts["tests"]
        or any(counts[k] for k in ("fail", "cancelled", "skipped", "todo"))
    ):
        raise ValueError(f"Incomplete Node suite: {counts}")
    return counts


def visual_summary(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    cases = report.get("cases", [])
    if (
        report.get("passed") is not True
        or report.get("syntheticOnly") is not True
        or len(cases) != 48
        or len({case.get("name") for case in cases}) != 48
        or any(case.get("passed") is not True for case in cases)
        or report.get("unexpectedRequests") != []
        or report.get("blockedExternalRequests") != []
        or report.get("playwright") != "1.65.1"
        or report.get("chromium") != "159.0.8154.6"
    ):
        raise ValueError(
            "Visual QA must contain all 48 passing synthetic cases with the pinned browser and no unexpected requests"
        )
    return {
        "total": len(cases),
        "passed": len(cases),
        "failed": [],
        "skipped": [],
        "chromium": report["chromium"],
    }


class Plan:
    def __init__(self, root: Path):
        self.root = root
        self.directory = root / "reports/ci"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.work = self.directory / ".work"
        self.work.mkdir(exist_ok=True)
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith(("RCLONE_", "CM_", "ASC_", "IOS_")) or re.search(
                r"(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)", key
            ):
                self.env.pop(key)
        self.env.update(
            CI="true",
            PYTHONUTF8="1",
            PYTHONUNBUFFERED="1",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            RCLONE_CONFIG=str(self.work / "empty-rclone.conf"),
            RCLONE_ASK_PASSWORD="false",
        )
        self.report = {
            "schemaVersion": 1,
            "profile": "rclone",
            "sha": os.environ.get("PRUEFDIENST_SHA"),
            "startedAt": time.time(),
            "gates": [
                {"id": key, "status": "not_run", "durationMs": 0} for key in GATES
            ],
            "boundaries": [
                "Disposable Linux container; no deployment or live backup/account access",
                "Python 3.10–3.13, installer, DOM and synthetic Chromium rendering are covered here",
                "Native iOS builds, simulator tests and signing require the separate macOS gate",
                "The 200% visual cases approximate layout reflow, not browser UI or physical-device zoom",
            ],
        }
        self.save()
        print("[pruefdienst-plan] " + json.dumps(GATES), flush=True)

    def save(self):
        text = json.dumps(self.report, ensure_ascii=False, indent=2)
        if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
            raise ValueError("Quality report exceeds the central report size limit")
        target = self.root / REPORT
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)

    def item(self, key: str) -> dict:
        return next(gate for gate in self.report["gates"] if gate["id"] == key)

    def gate(self, key: str, action, requires=()):
        gate = self.item(key)
        if any(self.item(dep)["status"] != "passed" for dep in requires):
            gate["reason"] = "Prerequisite did not pass: " + ", ".join(requires)
        else:
            started = time.monotonic()
            print("Running gate: " + key, flush=True)
            try:
                details = action() or {}
                if any(name in details for name in ("id", "status", "durationMs")):
                    raise ValueError("Gate details cannot replace the recorded outcome")
                gate.update(details, status="passed")
            except Exception as error:
                gate.update(status="failed", reason=str(error)[:2000])
                print(f"FAILED {key}: {gate['reason']}", flush=True)
            gate["durationMs"] = round((time.monotonic() - started) * 1000)
        self.save()
        print(
            "[pruefdienst-gate] "
            + json.dumps({k: gate[k] for k in ("id", "status", "durationMs")}),
            flush=True,
        )

    def command(self, key, args, *, env=None, timeout=900, input_text=None):
        log = self.directory / f"{key}.log"
        with log.open("ab") as stream:
            start = stream.tell()
            process = subprocess.Popen(
                [str(arg) for arg in args],
                cwd=self.root,
                env={**self.env, **(env or {})},
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.communicate(
                    input=input_text.encode("utf-8")
                    if input_text is not None
                    else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RuntimeError(f"{key}: timed out after {timeout}s") from None
        raw = log.read_bytes()
        output = raw[start:].decode("utf-8", errors="replace")
        self.item(key)["output"] = raw[-8000:].decode("utf-8", errors="replace")
        print(output[-12000:] if process.returncode else output[-3000:], flush=True)
        if process.returncode:
            raise RuntimeError(
                f"{key}: exit {process.returncode}; see reports/ci/{key}.log"
            )
        return output

    def python(self, version: str) -> Path:
        return self.work / f"python-{version}/bin/python"


def runtime(plan: Plan) -> dict:
    if (
        sys.platform != "linux"
        or os.geteuid() == 0
        or os.environ.get("PRUEFDIENST_PROFILE") != "rclone"
    ):
        raise ValueError(
            "Run only as the nonprivileged user of the isolated rclone Pruefdienst container"
        )
    tools = (
        "git",
        "node",
        "npm",
        "bash",
        "shellcheck",
        "systemd-analyze",
        *(f"python{version}" for version in PYTHONS),
    )
    if missing := [tool for tool in tools if not shutil.which(tool)]:
        raise ValueError("Required image tools missing: " + ", ".join(missing))
    expected_sha = os.environ.get("PRUEFDIENST_SHA", "")
    actual_sha = plan.command("runtime", ["git", "rev-parse", "HEAD"]).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha) or actual_sha != expected_sha:
        raise ValueError(
            "Checked-out commit does not match the requested Pruefdienst SHA"
        )
    node = plan.command("runtime", ["node", "--version"]).strip()
    if not node.startswith("v24."):
        raise ValueError("Pinned Node 24 image required")
    versions = {}
    for version in PYTHONS:
        found = plan.command(
            "runtime",
            [
                f"python{version}",
                "-c",
                "import sys; print('.'.join(map(str,sys.version_info[:3])))",
            ],
        ).splitlines()[-1]
        if not found.startswith(version + "."):
            raise ValueError(f"python{version} resolves to {found}")
        versions[version] = found
    for executable, expected in (
        (LEGACY_RCLONE, "1.60.1"),
        (PRODUCTION_RCLONE, "1.74.4"),
    ):
        output = plan.command("runtime", [executable, "version"])
        suffix = r"(?:-DEV)?" if executable == LEGACY_RCLONE else ""
        if not re.search(
            rf"^rclone v{re.escape(expected)}{suffix}(?:\s|$)", output, re.MULTILINE
        ):
            raise ValueError(f"Unexpected rclone version at {executable}")
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        raise ValueError("Pinned Playwright browser directory missing from the image")
    return {
        "sha": actual_sha,
        "python": versions,
        "node": node,
        "rclone": ["1.60.1", "1.74.4"],
        "platform": sys.platform,
    }


def dependencies(plan: Plan, version: str) -> dict:
    key = f"python-dependencies-{version}"
    plan.command(
        key, [f"python{version}", "-m", "venv", plan.python(version).parents[1]]
    )
    python = plan.python(version)
    for arguments in (
        ["scripts/update_dependency_lock.py", "--check"],
        [
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            "requirements.lock",
        ],
        ["-m", "pip", "install", "-r", "requirements-dev.txt"],
        ["-m", "pip", "check"],
    ):
        plan.command(key, [python, *arguments])
    return {
        "python": version,
        "productionDependencies": "requirements.lock (hash checked wheels only)",
    }


def controller_tests(plan: Plan) -> dict:
    destination = plan.work / "controller-tests"
    plan.command(
        "central-controller",
        [
            "python3",
            "infra/pruefdienst/central/stage.py",
            "--source",
            "/opt/rclone-ci/controller",
            "--destination",
            destination,
        ],
    )
    tests = sorted(destination.glob("*.unit.test.cjs"))
    if not tests:
        raise ValueError("Staged controller unit suites are missing")
    output = plan.command(
        "central-controller", ["node", "--test", "--test-reporter=tap", *tests]
    )
    return {"tests": node_summary(output), "suites": [file.name for file in tests]}


def quality(plan: Plan, version: str):
    key = f"python-quality-{version}"
    sources = [
        "app",
        "tests",
        "scripts/runtime_files.py",
        "scripts/ios_release_preflight.py",
        "scripts/ci",
        "infra/pruefdienst",
    ]
    for command in ("check", "format"):
        arguments = [plan.python(version), "-m", "ruff", command]
        plan.command(
            key, [*arguments, *(["--check"] if command == "format" else []), *sources]
        )
    plan.command(
        key,
        [
            plan.python(version),
            "-c",
            "from scripts.runtime_files import supported; assert supported(), 'POSIX no-follow directory handles are required'",
        ],
    )


def python_tests(
    plan: Plan, version: str, *, rclone=PRODUCTION_RCLONE, key=None
) -> dict:
    key = key or f"python-tests-{version}"
    xml = plan.directory / f"{key}.xml"
    args = [plan.python(version), "-m", "pytest", "-q", "-r", "sx", f"--junitxml={xml}"]
    oauth = key.startswith("oauth-")
    if oauth:
        args.extend(["tests/test_remote_setup.py", "-k", "real_own_drive"])
    error = None
    try:
        plan.command(
            key,
            args,
            env={"PATH": str(rclone.parent) + os.pathsep + plan.env.get("PATH", "")},
            timeout=1200,
        )
    except RuntimeError as caught:
        error = caught
    summary = junit_summary(
        xml, expected=2 if oauth else None, minimum=1 if oauth else 1704
    )
    plan.item(key)["tests"] = summary
    if error or summary["failed"] or summary["skipped"] or summary["passed"] <= 0:
        raise ValueError(
            f"Python suite incomplete: {summary['passed']} passed, {len(summary['failed'])} failed, {len(summary['skipped'])} skipped"
        ) from error
    return {"tests": summary, "rclone": str(rclone)}


def installer(plan: Plan):
    plan.command("installer", ["bash", "-n", "scripts/install.sh"])
    plan.command("installer", ["shellcheck", "scripts/install.sh"])
    for file in sorted(
        {plan.root / "app/static/app.js", *plan.root.glob("app/static/app-*.js")}
    ):
        plan.command(
            "installer",
            ["node", "--input-type=module", "--check"],
            input_text=file.read_text(encoding="utf-8"),
        )
    for file in ("app/static/workspace.js", "app/static/sw.js"):
        plan.command("installer", ["node", "--check", file])
    # The image pre-creates the user and the /opt/rclone-sync -> /work/repo link.
    # These inert executable stubs preserve the former GH validation of all five
    # unchanged production units. No daemon is started or root privilege needed.
    if Path("/opt/rclone-sync").resolve() != plan.root.resolve():
        raise ValueError(
            "Systemd verification requires the image's /opt/rclone-sync repository link"
        )
    for directory in ("venv/bin", "data", "logs", "temp"):
        (plan.root / directory).mkdir(parents=True, exist_ok=True)
    for binary in ("python", "uvicorn"):
        target = plan.root / "venv/bin" / binary
        shutil.copyfile("/bin/true", target)
        target.chmod(0o755)
    plan.command(
        "installer",
        ["systemd-analyze", "verify", *(f"systemd/{unit}" for unit in UNITS)],
    )


def web_dependencies(plan: Plan):
    plan.command(
        "web-dependencies",
        [
            "npm",
            "ci",
            "--prefix",
            "tests/web",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
    )


def web_behavior(plan: Plan) -> dict:
    # Separate commands retain each independent test summary in the report.
    suites = {}
    for script in ("runtime-behavior.cjs", "review-regressions.cjs"):
        output = plan.command(
            "web-behavior",
            ["node", "--test", "--test-reporter=tap", f"tests/web/{script}"],
        )
        suites[script] = node_summary(output)
    for script, marker in (
        ("workspace-dom.cjs", "Workspace DOM checks passed:"),
        ("workspace-alpine.cjs", "Workspace Alpine VM checks passed:"),
    ):
        output = plan.command("web-behavior", ["node", f"tests/web/{script}"])
        if marker not in output:
            raise ValueError(
                "Missing successful workspace integration result: " + script
            )
        suites[script] = {"status": "passed"}
    return {"suites": suites}


def web_visual(plan: Plan) -> dict:
    output = plan.directory / "web-visual"
    error = None
    try:
        plan.command(
            "web-visual",
            ["npm", "run", "test:visual", "--prefix", "tests/web"],
            env={"UI_VISUAL_ISOLATED": "1", "UI_VISUAL_OUTPUT": str(output)},
            timeout=900,
        )
    except RuntimeError as caught:
        error = caught
    summary = visual_summary(output / "report.json")
    plan.item("web-visual")["tests"] = summary
    if error:
        raise error
    return {"tests": summary}


def evidence(plan: Plan) -> dict:
    files = [
        *plan.directory.glob("*.xml"),
        *plan.directory.glob("*.log"),
        *plan.directory.glob("web-visual/*"),
    ]
    names = []
    archive = plan.root / "reports/rclone-ci-evidence.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for file in sorted(files):
            if (
                file.is_symlink()
                or not file.is_file()
                or not file.resolve().is_relative_to(plan.directory.resolve())
            ):
                raise ValueError("Evidence contains an unsafe or unexpected file")
            if file.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Evidence file exceeds size limit: " + file.name)
            name = file.relative_to(plan.directory).as_posix()
            bundle.write(file, name)
            names.append(name)
    if plan.item("web-visual")["status"] == "passed":
        visual_files = [name for name in names if name.startswith("web-visual/")]
        if (
            sum(name.endswith(".png") for name in visual_files) != 48
            or sum(name.endswith("-trace.zip") for name in visual_files) != 6
        ):
            raise ValueError(
                "Successful visual QA must preserve 48 screenshots and six synthetic traces"
            )
    for key in (f"python-tests-{version}" for version in PYTHONS):
        if plan.item(key)["status"] == "passed" and f"{key}.xml" not in names:
            raise ValueError("Missing passed-suite JUnit evidence: " + key)
    size = archive.stat().st_size
    if not names or size > MAX_ARCHIVE_BYTES:
        raise ValueError("Missing or oversized CI evidence archive")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    plan.report["evidenceFiles"] = names
    plan.report["artifacts"] = [
        {
            "name": "rclone-ci-evidence.zip",
            "path": "reports/rclone-ci-evidence.zip",
            "encoding": "binary",
            "sha256": digest.hexdigest(),
            "bytes": size,
            "files": len(names),
        }
    ]
    return {"files": len(names), "archiveBytes": size}


def main() -> int:
    plan = Plan(ROOT)
    plan.gate("runtime", lambda: runtime(plan))
    plan.gate("central-controller", lambda: controller_tests(plan), ("runtime",))
    for version in PYTHONS:
        dependencies_key = f"python-dependencies-{version}"
        plan.gate(
            dependencies_key,
            lambda version=version: dependencies(plan, version),
            ("runtime",),
        )
        plan.gate(
            f"python-quality-{version}",
            lambda version=version: quality(plan, version),
            (dependencies_key,),
        )
        plan.gate(
            f"python-tests-{version}",
            lambda version=version: python_tests(plan, version),
            (dependencies_key,),
        )
    for name, executable in (
        ("legacy", LEGACY_RCLONE),
        ("production", PRODUCTION_RCLONE),
    ):
        key = f"oauth-{name}"
        plan.gate(
            key,
            lambda key=key, executable=executable: python_tests(
                plan, "3.12", rclone=executable, key=key
            ),
            ("python-dependencies-3.12",),
        )
    plan.gate("installer", lambda: installer(plan), ("runtime",))
    plan.gate("web-dependencies", lambda: web_dependencies(plan), ("runtime",))
    plan.gate("web-behavior", lambda: web_behavior(plan), ("web-dependencies",))
    plan.gate("web-visual", lambda: web_visual(plan), ("web-dependencies",))
    plan.gate("evidence", lambda: evidence(plan))
    plan.report["finishedAt"] = time.time()
    plan.report["passed"] = all(
        gate["status"] == "passed" for gate in plan.report["gates"]
    )
    plan.save()
    print(
        "CI: "
        + (
            "all gates passed"
            if plan.report["passed"]
            else "FAILED: "
            + ", ".join(
                gate["id"]
                for gate in plan.report["gates"]
                if gate["status"] != "passed"
            )
        ),
        flush=True,
    )
    return 0 if plan.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
