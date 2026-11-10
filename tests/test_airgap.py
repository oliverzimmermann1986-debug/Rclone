"""Tests für Airgap-USB (app/airgap.py); udisksctl/findmnt sind gemockt."""

import subprocess
from types import SimpleNamespace

import pytest

from app import airgap

DEV = "/dev/disk/by-uuid/ABCD-1234"


def proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRun:
    """Antwortet je Kommando (z. B. 'mount', 'unmount', 'findmnt') und merkt sich die Aufrufe."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["capture_output"] and kwargs["text"]
        self.calls.append((cmd, kwargs["timeout"]))
        key = cmd[0] if cmd[0] == "findmnt" else cmd[1]
        answer = self.answers.get(key, proc())
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def verbs(self):
        return [c[0][1] if c[0][0] == "udisksctl" else c[0][0] for c in self.calls]


@pytest.mark.parametrize(
    "device",
    [
        "",
        None,
        "sdb1",
        "/dev",
        "/mnt/usb",
        "/dev/sd b1",
        "/dev/sdb1\n",
        "/dev/sdb1\x00",
        "/dev/sdb1 ",
        "/dev/../etc/passwd",
        "/dev/" + "a" * 252,
    ],
)
def test_validate_device_rejects(device):
    with pytest.raises(ValueError, match="Airgap-Gerät"):
        airgap.validate_device(device)


@pytest.mark.parametrize("device", [DEV, "/dev/sdb1", "/dev/" + "a" * 251])
def test_validate_device_accepts(device):
    assert airgap.validate_device(device) == device


def test_device_mountpoint():
    run = FakeRun(findmnt=proc(stdout="/media/x\n/mnt/bind\n"))
    assert airgap.device_mountpoint(DEV, run=run) == "/media/x"
    assert run.calls[0][0] == ["findmnt", "-n", "-o", "TARGET", "--source", DEV]
    assert airgap.device_mountpoint(DEV, run=FakeRun(findmnt=proc(1))) is None
    with pytest.raises(RuntimeError, match="findmnt nicht installiert"):
        airgap.device_mountpoint(DEV, run=FakeRun(findmnt=FileNotFoundError()))


@pytest.mark.parametrize(
    "stdout",
    [
        "Mounted /dev/sdb1 at /media/user/My Disk.\n",
        "Mounted /dev/sdb1 at /media/user/My Disk\n",
    ],
)
def test_mount_parses_both_stdout_variants(stdout):
    run = FakeRun(mount=proc(stdout=stdout))
    assert airgap.mount_device(DEV, run=run) == "/media/user/My Disk"
    assert run.calls == [
        (["udisksctl", "mount", "-b", DEV, "--no-user-interaction"], 60)
    ]


def test_mount_falls_back_to_findmnt_when_stdout_unparsable():
    run = FakeRun(mount=proc(stdout="ok"), findmnt=proc(stdout="/media/y\n"))
    assert airgap.mount_device(DEV, run=run) == "/media/y"


def test_mount_already_mounted_returns_existing_mountpoint():
    run = FakeRun(
        mount=proc(1, stderr="GDBus.Error:...UDisks2.Error.AlreadyMounted: ..."),
        findmnt=proc(stdout="/media/z\n"),
    )
    assert airgap.mount_device(DEV, run=run) == "/media/z"


def test_mount_failure_has_short_stderr():
    run = FakeRun(mount=proc(1, stderr="Fehler " + "x" * 500))
    with pytest.raises(RuntimeError) as exc:
        airgap.mount_device(DEV, run=run)
    assert "konnte nicht eingebunden werden" in str(exc.value)
    assert len(str(exc.value)) < 400


def test_mount_unknown_mountpoint_fails():
    run = FakeRun(mount=proc(stdout="ok"), findmnt=proc(1))
    with pytest.raises(RuntimeError, match="nicht ermittelbar"):
        airgap.mount_device(DEV, run=run)


def test_missing_udisksctl_gives_german_message():
    run = FakeRun(mount=FileNotFoundError())
    with pytest.raises(RuntimeError) as exc:
        airgap.mount_device(DEV, run=run)
    assert str(exc.value) == "udisksctl nicht installiert (Paket udisks2)"


def test_mount_timeout_is_runtime_error():
    run = FakeRun(mount=subprocess.TimeoutExpired("udisksctl", 60))
    with pytest.raises(RuntimeError, match="Zeitüberschreitung nach 60 s"):
        airgap.mount_device(DEV, run=run)


def test_eject_success_runs_unmount_then_power_off():
    run = FakeRun()
    assert airgap.eject_device(DEV, run=run) == {
        "unmounted": True,
        "powered_off": True,
        "error": None,
    }
    assert run.calls == [
        (["udisksctl", "unmount", "-b", DEV, "--no-user-interaction"], 120),
        (["udisksctl", "power-off", "-b", DEV, "--no-user-interaction"], 120),
    ]


def test_eject_partial_failure_on_power_off_is_reported():
    run = FakeRun(**{"power-off": proc(1, stderr="Device is busy")})
    result = airgap.eject_device(DEV, run=run)
    assert result["unmounted"] is True
    assert result["powered_off"] is False
    assert "Power-off fehlgeschlagen: Device is busy" in result["error"]


def test_eject_unmount_failure_skips_power_off():
    run = FakeRun(unmount=proc(1, stderr="target is busy"))
    result = airgap.eject_device(DEV, run=run)
    assert (result["unmounted"], result["powered_off"]) == (False, False)
    assert "Unmount fehlgeschlagen: target is busy" in result["error"]
    assert run.verbs == ["unmount"]


def test_eject_not_mounted_still_powers_off():
    run = FakeRun(unmount=proc(1, stderr="...UDisks2.Error.NotMounted: ..."))
    result = airgap.eject_device(DEV, run=run)
    assert (result["unmounted"], result["powered_off"], result["error"]) == (
        True,
        True,
        None,
    )


def test_eject_never_raises():
    result = airgap.eject_device(DEV, run=FakeRun(unmount=FileNotFoundError()))
    assert result["error"] == "udisksctl nicht installiert (Paket udisks2)"
    assert airgap.eject_device("kein-geraet", run=FakeRun())["error"]


def test_session_without_device_is_inactive():
    run = FakeRun()
    for pair in ({"local": "/x"}, {"local": "/x", "airgap_device": ""}):
        with airgap.airgap_session(pair, run=run) as info:
            assert info == {"active": False}
    assert run.calls == []


def test_session_invalid_device_raises_before_any_command():
    run = FakeRun()
    with pytest.raises(ValueError):
        with airgap.airgap_session({"airgap_device": "sdb1"}, run=run):
            pass
    assert run.calls == []


def test_session_mounts_yields_and_ejects(tmp_path):
    mount = tmp_path / "usb"
    (mount / "backup").mkdir(parents=True)
    run = FakeRun(mount=proc(stdout=f"Mounted /dev/sdb1 at {mount}.\n"))
    pair = {"local": str(mount / "backup"), "airgap_device": DEV}
    with airgap.airgap_session(pair, run=run) as info:
        assert info["active"] is True
        assert info["device"] == DEV
        assert info["mountpoint"] == str(mount)
        assert "eject" not in info
    assert info["eject"] == {"unmounted": True, "powered_off": True, "error": None}
    assert run.verbs == ["mount", "unmount", "power-off"]


def test_session_local_equal_to_mountpoint_is_allowed(tmp_path):
    run = FakeRun(mount=proc(stdout=f"Mounted /dev/sdb1 at {tmp_path}\n"))
    with airgap.airgap_session(
        {"local": str(tmp_path), "airgap_device": DEV}, run=run
    ) as info:
        assert info["active"] is True


@pytest.mark.parametrize("local", ["sibling", "usb-evil", "", None])
def test_session_path_outside_mountpoint_aborts_and_ejects(tmp_path, local):
    mount = tmp_path / "usb"
    run = FakeRun(mount=proc(stdout=f"Mounted /dev/sdb1 at {mount}\n"))
    pair = {
        "local": str(tmp_path / local) if local else local,
        "airgap_device": DEV,
    }
    entered = False
    with pytest.raises(RuntimeError, match="Lokaler Pfad liegt nicht auf dem Airgap"):
        with airgap.airgap_session(pair, run=run):
            entered = True
    assert not entered
    assert run.verbs == ["mount", "unmount", "power-off"]


def test_session_ejects_when_job_raises(tmp_path):
    run = FakeRun(mount=proc(stdout=f"Mounted /dev/sdb1 at {tmp_path}\n"))
    pair = {"local": str(tmp_path), "airgap_device": DEV}
    with pytest.raises(KeyError):
        with airgap.airgap_session(pair, run=run) as info:
            raise KeyError("job kaputt")
    assert info["eject"]["powered_off"] is True
    assert run.verbs == ["mount", "unmount", "power-off"]


def test_session_reports_partial_eject_failure(tmp_path):
    run = FakeRun(
        mount=proc(stdout=f"Mounted /dev/sdb1 at {tmp_path}\n"),
        **{"power-off": proc(1, stderr="busy")},
    )
    pair = {"local": str(tmp_path), "airgap_device": DEV}
    with airgap.airgap_session(pair, run=run) as info:
        pass
    assert info["eject"]["unmounted"] is True
    assert info["eject"]["powered_off"] is False
    assert "busy" in info["eject"]["error"]


def test_session_missing_udisksctl(tmp_path):
    run = FakeRun(mount=FileNotFoundError())
    with pytest.raises(
        RuntimeError, match=r"udisksctl nicht installiert \(Paket udisks2\)"
    ):
        with airgap.airgap_session({"local": "/x", "airgap_device": DEV}, run=run):
            pass
