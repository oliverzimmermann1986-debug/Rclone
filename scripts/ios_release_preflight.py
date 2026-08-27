"""Fail-closed provenance checks for signed iOS release tags."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

if __package__:
    from .ios_release_version import version_from_tag
else:  # pragma: no cover - exercised by the Codemagic script entry point
    from ios_release_version import version_from_tag


class ReleaseSourceError(ValueError):
    """Raised when the release checkout is not trusted for publishing."""


GitRunner = Callable[
    [Sequence[str], Path], subprocess.CompletedProcess[str]
]


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


def verify_release_source(
    repository: Path,
    tag: str,
    *,
    runner: GitRunner = _run_git,
) -> str:
    """Return the verified release commit or reject the checkout.

    The caller must fetch ``origin/main`` and the exact remote tag first. This
    function deliberately performs no network access and never includes git's
    potentially sensitive stderr in raised errors.
    """

    version_from_tag(tag)
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

    signature = runner(["verify-tag", "--raw", tag_ref], repository)
    if signature.returncode != 0:
        raise ReleaseSourceError(
            "Release-Tag konnte nicht kryptografisch verifiziert werden"
        )

    tag_commit = _git_output(
        ["rev-parse", "--verify", f"{tag_ref}^{{commit}}"],
        repository,
        runner,
        "Release-Tag verweist nicht auf einen Commit",
    )
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

    main_commit = _git_output(
        ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
        repository,
        runner,
        "Explizit geholter origin/main-Commit fehlt",
    )
    ancestry = runner(
        ["merge-base", "--is-ancestor", tag_commit, main_commit], repository
    )
    if ancestry.returncode != 0:
        raise ReleaseSourceError(
            "Release-Commit stammt nicht von origin/main ab"
        )

    return tag_commit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()

    try:
        commit = verify_release_source(args.repository, args.tag)
    except (OSError, ReleaseSourceError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Release-Herkunft verifiziert: {commit[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
