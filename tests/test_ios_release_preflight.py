from __future__ import annotations

import subprocess
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.ios_release_preflight import (
    MAX_TAG_OBJECT_BYTES,
    ReleaseSourceError,
    verify_release_source,
)

TAG_OBJECT = "d" * 40
RELEASE_FINGERPRINT = "A" * 40
SUBKEY_FINGERPRINT = "B" * 40
OTHER_FINGERPRINT = "C" * 40


def _signature_status(*, signer=RELEASE_FINGERPRINT, primary=RELEASE_FINGERPRINT):
    return (
        "[GNUPG:] NEWSIG\n"
        f"[GNUPG:] KEY_CONSIDERED {primary} 0\n"
        f"[GNUPG:] GOODSIG {signer[-16:]} Release fixture\n"
        f"[GNUPG:] VALIDSIG {signer} 2020-01-01 1577836800 0 4 0 1 8 00 {primary}\n"
        "[GNUPG:] TRUST_UNDEFINED 0 pgp\n"
    )


def _tag_content(*, name="ios-v1.2.3", object_type="commit", extra_header=""):
    return (
        f"object {'a' * 40}\ntype {object_type}\ntag {name}\n"
        f"tagger Fixture <release@example.invalid> 1 +0000\n{extra_header}\n"
        "Release fixture\n-----BEGIN PGP SIGNATURE-----\ndummy\n"
        "-----END PGP SIGNATURE-----\n"
    )


class FakeGit:
    def __init__(
        self,
        responses: dict[tuple[str, ...], tuple[int, str] | tuple[int, str, str] | int],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, args: Sequence[str], _repository: Path
    ) -> subprocess.CompletedProcess[str]:
        key = tuple(args)
        self.calls.append(key)
        response = self.responses.get(key, (1, ""))
        if isinstance(response, tuple):
            code, output = response[:2]
            stderr = response[2] if len(response) == 3 else "hidden"
        else:
            code, output, stderr = response, "", "hidden"
        return subprocess.CompletedProcess(["git", *args], code, output, stderr)


def _valid_git() -> FakeGit:
    commit = "a" * 40
    return FakeGit(
        {
            ("cat-file", "-t", "refs/tags/ios-v1.2.3"): (0, "tag\n"),
            ("rev-parse", "--verify", "refs/tags/ios-v1.2.3"): (0, TAG_OBJECT),
            ("cat-file", "-s", TAG_OBJECT): (0, str(len(_tag_content()))),
            ("cat-file", "tag", TAG_OBJECT): (0, _tag_content()),
            ("verify-tag", "--raw", TAG_OBJECT): (0, "", _signature_status()),
            (
                "rev-parse",
                "--verify",
                f"{TAG_OBJECT}^{{commit}}",
            ): (0, f"{commit}\n"),
            ("rev-parse", "--verify", "HEAD^{commit}"): (0, f"{commit}\n"),
            (
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            ): (0, f"{'b' * 40}\n"),
            ("merge-base", "--is-ancestor", commit, "b" * 40): 0,
        }
    )


def test_accepts_signed_annotated_tag_on_checked_out_main_history(tmp_path: Path):
    git = _valid_git()

    assert (
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )
        == "a" * 40
    )
    assert ("verify-tag", "--raw", TAG_OBJECT) in git.calls
    assert ("merge-base", "--is-ancestor", "a" * 40, "b" * 40) in git.calls


def test_accepts_signing_subkey_only_when_bound_to_expected_primary(tmp_path):
    git = _valid_git()
    git.responses[("verify-tag", "--raw", TAG_OBJECT)] = (
        0,
        "",
        _signature_status(signer=SUBKEY_FINGERPRINT),
    )
    assert (
        verify_release_source(
            tmp_path,
            "ios-v1.2.3",
            expected_fingerprint=RELEASE_FINGERPRINT.lower(),
            runner=git,
        )
        == "a" * 40
    )


@pytest.mark.parametrize(
    "status",
    [
        "",
        f"[GNUPG:] GOODSIG {RELEASE_FINGERPRINT[-16:]} Fixture\n",
        f"[GNUPG:] VALIDSIG {RELEASE_FINGERPRINT} 2020-01-01 1577836800 0 4 0 1 8 00 {RELEASE_FINGERPRINT}\n",
        _signature_status(signer=OTHER_FINGERPRINT, primary=OTHER_FINGERPRINT)
        + f"[GNUPG:] KEY_CONSIDERED {RELEASE_FINGERPRINT} 0\n",
        _signature_status(signer=OTHER_FINGERPRINT, primary=OTHER_FINGERPRINT).replace(
            "Release fixture", RELEASE_FINGERPRINT
        ),
        _signature_status(primary=OTHER_FINGERPRINT),
        _signature_status()
        + _signature_status(signer=OTHER_FINGERPRINT, primary=OTHER_FINGERPRINT),
        _signature_status() + f"[GNUPG:] GOODSIG {OTHER_FINGERPRINT[-16:]} Other\n",
        _signature_status().replace("[GNUPG:] VALIDSIG ", "[GNUPG:]VALIDSIG "),
        _signature_status().replace(" 2020-01-01 ", " 2020-02-30 "),
        _signature_status().replace(" 1577836800 ", " not-a-timestamp "),
        _signature_status().replace(" 1577836800 0 ", " 1577836800 1 "),
        _signature_status().replace(" 4 0 1 8 00 ", " 4 1 1 8 00 "),
        _signature_status().replace(f"00 {RELEASE_FINGERPRINT}", "00"),
        _signature_status().replace(
            f"00 {RELEASE_FINGERPRINT}", f"00 {RELEASE_FINGERPRINT} extra"
        ),
        _signature_status().replace(
            f"GOODSIG {RELEASE_FINGERPRINT[-16:]}", f"GOODSIG {OTHER_FINGERPRINT[-16:]}"
        ),
        _signature_status().replace("[GNUPG:] GOODSIG", "[GNUPG:] GOODSIG_MALFORMED"),
        _signature_status().replace(
            "[GNUPG:] NEWSIG\n", "[GNUPG:] NEWSIG\n[GNUPG:] NEWSIG\n"
        ),
    ],
)
def test_rejects_missing_malformed_ambiguous_or_wrong_signer_status(tmp_path, status):
    git = _valid_git()
    git.responses[("verify-tag", "--raw", TAG_OBJECT)] = (0, "", status)
    with pytest.raises(ReleaseSourceError, match="Signaturstatus"):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )
    assert ("rev-parse", "--verify", "HEAD^{commit}") not in git.calls


@pytest.mark.parametrize(
    "status",
    (
        "EXPSIG",
        "EXPKEYSIG",
        "REVKEYSIG",
        "KEYEXPIRED",
        "SIGEXPIRED",
        "KEYREVOKED",
        "ERRSIG",
        "BADSIG",
        "NO_PUBKEY",
        "NODATA",
        "FAILURE",
        "ERROR",
        "UNEXPECTED",
        "BADARMOR",
        "TRUST_NEVER",
    ),
)
def test_error_expiry_and_revocation_status_override_validsig_even_on_git_success(
    tmp_path, status
):
    git = _valid_git()
    git.responses[("verify-tag", "--raw", TAG_OBJECT)] = (
        0,
        "",
        _signature_status() + f"[GNUPG:] {status} fixture\n",
    )
    with pytest.raises(ReleaseSourceError, match="Signaturstatus"):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )


def test_signature_status_on_stdout_does_not_prove_the_signer(tmp_path):
    git = _valid_git()
    git.responses[("verify-tag", "--raw", TAG_OBJECT)] = (0, _signature_status(), "")
    with pytest.raises(ReleaseSourceError, match="Signaturstatus"):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )


@pytest.mark.parametrize(
    "fingerprint", (None, "", "not-a-fingerprint", "A" * 16, "G" * 40)
)
def test_invalid_required_fingerprint_is_rejected_before_git(tmp_path, fingerprint):
    git = _valid_git()
    with pytest.raises(ReleaseSourceError, match="Fingerprint"):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=fingerprint, runner=git
        )
    assert git.calls == []


def test_unpinned_library_call_is_not_available(tmp_path):
    git = _valid_git()
    with pytest.raises(TypeError, match="expected_fingerprint"):
        verify_release_source(tmp_path, "ios-v1.2.3", runner=git)
    assert git.calls == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda responses: responses.__setitem__(
                ("cat-file", "-t", "refs/tags/ios-v1.2.3"), (0, "commit\n")
            ),
            "annotiert",
        ),
        (
            lambda responses: responses.__setitem__(
                ("verify-tag", "--raw", TAG_OBJECT), 1
            ),
            "kryptografisch",
        ),
        (
            lambda responses: responses.__setitem__(
                ("rev-parse", "--verify", "HEAD^{commit}"), (0, f"{'c' * 40}\n")
            ),
            "unterschiedliche",
        ),
        (
            lambda responses: responses.__setitem__(
                ("merge-base", "--is-ancestor", "a" * 40, "b" * 40), 1
            ),
            "origin/main",
        ),
    ],
)
def test_rejects_untrusted_release_sources(tmp_path: Path, mutate, message: str):
    git = _valid_git()
    mutate(git.responses)

    with pytest.raises(ReleaseSourceError, match=message):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )


def test_rejects_invalid_tag_before_invoking_git(tmp_path: Path):
    git = _valid_git()

    with pytest.raises(ValueError):
        verify_release_source(
            tmp_path,
            "ios-v1.2.3-beta",
            expected_fingerprint=RELEASE_FINGERPRINT,
            runner=git,
        )

    assert git.calls == []


@pytest.mark.parametrize(
    "content,message",
    [
        (_tag_content(name="ios-v1.2.2"), "Signierter Tag-Name"),
        (_tag_content(object_type="tag"), "direkt auf einen Commit"),
        (_tag_content(extra_header="tag ios-v1.2.2\n"), "mehrdeutig"),
        ("object " + "a" * 40, "Header ist ungültig"),
    ],
)
def test_rejects_aliased_or_ambiguous_tag_objects_before_signature(
    tmp_path, content, message
):
    git = _valid_git()
    git.responses[("cat-file", "tag", TAG_OBJECT)] = (0, content)
    with pytest.raises(ReleaseSourceError, match=message):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )
    assert ("verify-tag", "--raw", TAG_OBJECT) not in git.calls


@pytest.mark.parametrize("size", ("0", "-1", "invalid", str(MAX_TAG_OBJECT_BYTES + 1)))
def test_rejects_invalid_or_large_tag_before_reading_object(tmp_path, size):
    git = _valid_git()
    git.responses[("cat-file", "-s", TAG_OBJECT)] = (0, size)
    with pytest.raises(ReleaseSourceError):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )
    assert ("cat-file", "tag", TAG_OBJECT) not in git.calls


def test_rejects_commit_that_disagrees_with_the_signed_object_header(tmp_path):
    git = _valid_git()
    git.responses[("rev-parse", "--verify", f"{TAG_OBJECT}^{{commit}}")] = (0, "c" * 40)
    with pytest.raises(ReleaseSourceError, match="Commit-Header"):
        verify_release_source(
            tmp_path, "ios-v1.2.3", expected_fingerprint=RELEASE_FINGERPRINT, runner=git
        )


def test_explicit_main_commit_is_used_even_if_remote_tracking_ref_moves(tmp_path):
    git = _valid_git()
    pinned = "b" * 40
    git.responses[("rev-parse", "--verify", f"{pinned}^{{commit}}")] = (0, pinned)
    git.responses[("rev-parse", "--verify", "refs/remotes/origin/main^{commit}")] = (
        0,
        "c" * 40,
    )
    assert (
        verify_release_source(
            tmp_path,
            "ios-v1.2.3",
            expected_fingerprint=RELEASE_FINGERPRINT,
            runner=git,
            trusted_main_commit=pinned,
        )
        == "a" * 40
    )
    assert (
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main^{commit}",
    ) not in git.calls
    assert ("merge-base", "--is-ancestor", "a" * 40, pinned) in git.calls


def test_invalid_explicit_main_commit_is_rejected_before_git(tmp_path):
    git = _valid_git()
    with pytest.raises(ReleaseSourceError, match="Gepinnter"):
        verify_release_source(
            tmp_path,
            "ios-v1.2.3",
            expected_fingerprint=RELEASE_FINGERPRINT,
            runner=git,
            trusted_main_commit="origin/main",
        )
    assert git.calls == []


def test_isolated_bootstrap_loads_only_its_explicit_version_helper(
    tmp_path, monkeypatch
):
    source = Path(__file__).resolve().parents[1] / "scripts"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    for name in ("ios_release_preflight.py", "ios_release_version.py"):
        shutil.copyfile(source / name, trusted / name)
    unverified = tmp_path / "unverified-checkout"
    unverified.mkdir()
    for name in ("ios_release_version.py", "sitecustomize.py"):
        (unverified / name).write_text(
            "raise RuntimeError('unverified checkout was imported')\n", encoding="utf-8"
        )
    monkeypatch.setenv("PYTHONPATH", str(unverified))
    result = subprocess.run(
        [sys.executable, "-I", str(trusted / "ios_release_preflight.py"), "--help"],
        cwd=unverified,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--trusted-main-commit" in result.stdout
    assert "--expected-fingerprint" in result.stdout


def test_cli_requires_fingerprint_before_running_git(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "ios_release_preflight.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            "ios-v1.2.3",
            "--repository",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "--expected-fingerprint" in result.stderr


def test_codemagic_bootstrap_uses_private_external_pinned_main_sources():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / "codemagic.yaml").read_text(encoding="utf-8"))
    scripts = workflow["workflows"]["ios-testflight"]["scripts"]
    gate = next(
        item["script"]
        for item in scripts
        if item["name"] == "Verify signed release provenance"
    )
    assert 'mktemp -d "/tmp/sicherpfad-release.XXXXXX"' in gate
    assert "TMPDIR" not in gate
    assert 'GNUPGHOME="$PROVENANCE_DIR/gnupg"' in gate
    assert "$CM_BUILD_DIR/.tools" not in gate
    assert 'python3 "$CM_BUILD_DIR/scripts/' not in gate
    assert gate.count('TRUSTED_MAIN_COMMIT="$(') == 1
    assert '"$TRUSTED_MAIN_COMMIT:scripts/ios_release_preflight.py"' in gate
    assert '"$TRUSTED_MAIN_COMMIT:scripts/ios_release_version.py"' in gate
    assert '--trusted-main-commit "$TRUSTED_MAIN_COMMIT"' in gate
    assert '--expected-fingerprint "$IOS_RELEASE_GPG_FINGERPRINT"' in gate
    execution = gate.index('python3 -I "$PROVENANCE_DIR/ios_release_preflight.py"')
    assert (
        gate.index('"$TRUSTED_MAIN_COMMIT:scripts/ios_release_version.py"') < execution
    )
    assert "trap 'rm -rf -- \"$PROVENANCE_DIR\"' EXIT" in gate
