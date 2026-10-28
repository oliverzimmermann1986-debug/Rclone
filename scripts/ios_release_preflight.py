"""Fail-closed provenance checks for signed iOS release tags."""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

if __package__:
    from .ios_release_version import version_from_tag
else:  # pragma: no cover - exercised by the Codemagic script entry point
    # -I intentionally excludes the script directory and PYTHONPATH. Load only
    # the sibling copied from the same trusted main commit by the bootstrap.
    helper = Path(__file__).with_name("ios_release_version.py")
    specification = importlib.util.spec_from_file_location("release_version", helper)
    if specification is None or specification.loader is None:
        raise ImportError("Vertrauenswürdiger Release-Versionsprüfer fehlt")
    version_module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(version_module)
    version_from_tag = version_module.version_from_tag

OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
FINGERPRINT_RE = re.compile(r"(?:[0-9A-F]{40}|[0-9A-F]{64})\Z")
MAX_TAG_OBJECT_BYTES = 1024 * 1024
_ALLOWED_SIGNATURE_STATUS = frozenset(
    {
        "NEWSIG",
        "KEY_CONSIDERED",
        "SIG_ID",
        "GOODSIG",
        "VALIDSIG",
        "TRUST_UNDEFINED",
        "TRUST_MARGINAL",
        "TRUST_FULLY",
        "TRUST_ULTIMATE",
        "NOTATION_NAME",
        "NOTATION_FLAGS",
        "NOTATION_DATA",
        "POLICY_URL",
        "VERIFICATION_COMPLIANCE_MODE",
    }
)


class ReleaseSourceError(ValueError):
    """Raised when the release checkout is not trusted for publishing."""


GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _run_git(args: Sequence[str], repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_output(
    args: Sequence[str],
    repository: Path,
    runner: GitRunner,
    error: str,
) -> str:
    result = runner(args, repository)
    if result.returncode != 0:
        raise ReleaseSourceError(error)
    value = result.stdout.strip()
    if not value:
        raise ReleaseSourceError(error)
    return value


def _verify_pinned_signature(stderr: str, expected_fingerprint: str) -> None:
    """Bind an unambiguous successful OpenPGP signature to its primary key.

    Git --raw forwards GnuPG status records on stderr. VALIDSIG's final field
    identifies the primary key even when its bound signing subkey signed the
    tag. Neither a considered key nor a UID is evidence of the actual signer.
    """
    error = (
        "Release-Signaturstatus fehlt, ist ungültig oder nicht vom gepinnten Schlüssel"
    )
    if not isinstance(stderr, str) or len(stderr) > 65536:
        raise ReleaseSourceError(error)
    records: dict[str, list[list[str]]] = {}
    for line in stderr.splitlines():
        if not line.startswith("[GNUPG:] "):
            if line.startswith("[GNUPG") or "[GNUPG:" in line:
                raise ReleaseSourceError(error)
            continue
        fields = line[len("[GNUPG:] ") :].split()
        if not fields or fields[0] not in _ALLOWED_SIGNATURE_STATUS:
            # This also rejects expired/revoked signatures or keys, errors and
            # unknown status types, even if verify-tag returned zero.
            raise ReleaseSourceError(error)
        records.setdefault(fields[0], []).append(fields[1:])
    valid = records.get("VALIDSIG", [])
    good = records.get("GOODSIG", [])
    if len(valid) != 1 or len(good) != 1 or len(records.get("NEWSIG", [])) > 1:
        raise ReleaseSourceError(error)
    fields = valid[0]
    # OpenPGP supplies all nine signature fields and the primary fingerprint.
    if len(fields) != 10:
        raise ReleaseSourceError(error)
    (
        signer,
        creation_date,
        created,
        expires,
        version,
        reserved,
        pkalg,
        hashalg,
        sigclass,
        primary,
    ) = fields
    if (
        not FINGERPRINT_RE.fullmatch(signer)
        or not FINGERPRINT_RE.fullmatch(primary)
        or primary != expected_fingerprint
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", creation_date)
        or not all(
            re.fullmatch(r"[0-9]+", value)
            for value in (created, expires, version, pkalg, hashalg)
        )
        or reserved != "0"
        or not re.fullmatch(r"[0-9A-Fa-f]{2}", sigclass)
    ):
        raise ReleaseSourceError(error)
    try:
        date.fromisoformat(creation_date)
    except ValueError as exc:
        raise ReleaseSourceError(error) from exc
    if (
        int(version) == 0
        or int(pkalg) == 0
        or int(hashalg) == 0
        or (int(expires) != 0 and int(expires) <= time.time())
        or not good[0]
        or not re.fullmatch(r"(?:[0-9A-F]{16}|[0-9A-F]{40}|[0-9A-F]{64})", good[0][0])
        or not signer.endswith(good[0][0])
    ):
        raise ReleaseSourceError(error)


def verify_release_source(
    repository: Path,
    tag: str,
    *,
    expected_fingerprint: str,
    runner: GitRunner = _run_git,
    trusted_main_commit: str | None = None,
) -> str:
    """Return the verified release commit or reject the checkout.

    The caller must fetch ``origin/main`` and the exact remote tag first. This
    function deliberately performs no network access and never includes git's
    potentially sensitive stderr in raised errors.
    """

    version_from_tag(tag)
    if not isinstance(expected_fingerprint, str):
        raise ReleaseSourceError("Erwarteter Release-Schlüssel-Fingerprint fehlt")
    expected_fingerprint = expected_fingerprint.strip().upper()
    if not FINGERPRINT_RE.fullmatch(expected_fingerprint):
        raise ReleaseSourceError(
            "Erwarteter Release-Schlüssel-Fingerprint ist ungültig"
        )
    if trusted_main_commit is not None and not OBJECT_ID_RE.fullmatch(
        trusted_main_commit
    ):
        raise ReleaseSourceError("Gepinnter origin/main-Commit ist ungültig")
    repository = repository.resolve()
    tag_ref = f"refs/tags/{tag}"

    object_type = _git_output(
        ["cat-file", "-t", tag_ref],
        repository,
        runner,
        "Release-Tag fehlt oder ist ungültig",
    )
    if object_type != "tag":
        raise ReleaseSourceError(
            "Release-Tag muss annotiert und kryptografisch signiert sein"
        )

    tag_object = _git_output(
        ["rev-parse", "--verify", tag_ref],
        repository,
        runner,
        "Release-Tag-Objekt fehlt",
    )
    if not OBJECT_ID_RE.fullmatch(tag_object):
        raise ReleaseSourceError("Release-Tag-Objekt ist ungültig")
    size_text = _git_output(
        ["cat-file", "-s", tag_object],
        repository,
        runner,
        "Release-Tag-Größe konnte nicht gelesen werden",
    )
    try:
        object_size = int(size_text)
    except ValueError as exc:
        raise ReleaseSourceError("Release-Tag-Größe ist ungültig") from exc
    if not 0 < object_size <= MAX_TAG_OBJECT_BYTES:
        raise ReleaseSourceError("Release-Tag überschreitet das Größenlimit")
    content = _git_output(
        ["cat-file", "tag", tag_object],
        repository,
        runner,
        "Release-Tag-Header konnte nicht gelesen werden",
    )
    header, separator, _body = content.partition("\n\n")
    if not separator:
        raise ReleaseSourceError("Release-Tag-Header ist ungültig")
    fields: dict[str, str] = {}
    for line in header.splitlines():
        name, space, value = line.partition(" ")
        if not space or not name or name in fields:
            raise ReleaseSourceError("Release-Tag-Header ist ungültig oder mehrdeutig")
        fields[name] = value
    if fields.get("tag") != tag:
        raise ReleaseSourceError(
            "Signierter Tag-Name stimmt nicht mit dem angeforderten Release-Tag überein"
        )
    if fields.get("type") != "commit" or not OBJECT_ID_RE.fullmatch(
        fields.get("object", "")
    ):
        raise ReleaseSourceError("Release-Tag muss direkt auf einen Commit verweisen")

    signature = runner(["verify-tag", "--raw", tag_object], repository)
    if signature.returncode != 0:
        raise ReleaseSourceError(
            "Release-Tag konnte nicht kryptografisch verifiziert werden"
        )
    _verify_pinned_signature(signature.stderr, expected_fingerprint)

    tag_commit = _git_output(
        ["rev-parse", "--verify", f"{tag_object}^{{commit}}"],
        repository,
        runner,
        "Release-Tag verweist nicht auf einen Commit",
    )
    if tag_commit != fields["object"]:
        raise ReleaseSourceError("Release-Tag und Commit-Header stimmen nicht überein")
    head_commit = _git_output(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        repository,
        runner,
        "Build-Checkout ist kein gültiger Commit",
    )
    if tag_commit != head_commit:
        raise ReleaseSourceError(
            "Release-Tag und Build-Checkout verweisen auf unterschiedliche Commits"
        )

    main_ref = trusted_main_commit or "refs/remotes/origin/main"
    main_commit = _git_output(
        ["rev-parse", "--verify", f"{main_ref}^{{commit}}"],
        repository,
        runner,
        "Explizit geholter origin/main-Commit fehlt",
    )
    if trusted_main_commit is not None and main_commit != trusted_main_commit:
        raise ReleaseSourceError("Gepinnter origin/main-Commit stimmt nicht überein")
    ancestry = runner(
        ["merge-base", "--is-ancestor", tag_commit, main_commit], repository
    )
    if ancestry.returncode != 0:
        raise ReleaseSourceError("Release-Commit stammt nicht von origin/main ab")

    return tag_commit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--trusted-main-commit")
    parser.add_argument("--expected-fingerprint", required=True)
    args = parser.parse_args()

    try:
        commit = verify_release_source(
            args.repository,
            args.tag,
            expected_fingerprint=args.expected_fingerprint,
            trusted_main_commit=args.trusted_main_commit,
        )
    except (OSError, ReleaseSourceError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Release-Herkunft verifiziert: {commit[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
