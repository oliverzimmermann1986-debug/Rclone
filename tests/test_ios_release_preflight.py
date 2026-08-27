from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.ios_release_preflight import ReleaseSourceError, verify_release_source


class FakeGit:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str] | int]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, args: Sequence[str], _repository: Path
    ) -> subprocess.CompletedProcess[str]:
        key = tuple(args)
        self.calls.append(key)
        response = self.responses.get(key, (1, ""))
        code, output = response if isinstance(response, tuple) else (response, "")
        return subprocess.CompletedProcess(["git", *args], code, output, "hidden")


def _valid_git() -> FakeGit:
    commit = "a" * 40
    return FakeGit(
        {
            ("cat-file", "-t", "refs/tags/ios-v1.2.3"): (0, "tag\n"),
            ("verify-tag", "--raw", "refs/tags/ios-v1.2.3"): 0,
            (
                "rev-parse",
                "--verify",
                "refs/tags/ios-v1.2.3^{commit}",
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

    assert verify_release_source(tmp_path, "ios-v1.2.3", runner=git) == "a" * 40
    assert ("verify-tag", "--raw", "refs/tags/ios-v1.2.3") in git.calls
    assert ("merge-base", "--is-ancestor", "a" * 40, "b" * 40) in git.calls


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
                ("verify-tag", "--raw", "refs/tags/ios-v1.2.3"), 1
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
        verify_release_source(tmp_path, "ios-v1.2.3", runner=git)


def test_rejects_invalid_tag_before_invoking_git(tmp_path: Path):
    git = _valid_git()

    with pytest.raises(ValueError):
        verify_release_source(tmp_path, "ios-v1.2.3-beta", runner=git)

    assert git.calls == []
