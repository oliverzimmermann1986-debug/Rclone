from pathlib import Path

import pytest

from scripts.update_dependency_lock import (
    build_lock,
    read_requirements,
    validate_lock,
    wheel_hashes,
)


def metadata(*files):
    return {"info": {"name": "example", "version": "1.2.3"}, "urls": list(files)}


def wheel(digest="a" * 64, **kwargs):
    return {
        "filename": "example-1.2.3-py3-none-any.whl",
        "packagetype": "bdist_wheel",
        "digests": {"sha256": digest},
        **kwargs,
    }


def test_runtime_lock_matches_current_exact_pins_and_markers():
    root = Path(__file__).resolve().parents[1]
    pins = read_requirements((root / "requirements.txt").read_text(encoding="utf-8"))
    validate_lock(pins, (root / "requirements.lock").read_text(encoding="utf-8"))


def test_markers_extras_and_all_wheel_hashes_survive_generation():
    pins = read_requirements('example[feature]==1.2.3; sys_platform != "win32"\n')
    data = metadata(wheel("b" * 64), wheel("a" * 64), wheel("b" * 64))
    lock = build_lock(pins, lambda name, version: data)
    validate_lock(pins, lock)
    assert pins[0] in lock
    assert lock.count("--hash=sha256:") == 2
    assert lock.index("a" * 64) < lock.index("b" * 64)


def test_source_archives_and_yanked_wheels_are_not_trusted():
    source = {
        "filename": "example-1.2.3.tar.gz",
        "packagetype": "sdist",
        "digests": {"sha256": "c" * 64},
    }
    assert wheel_hashes(
        "example", "1.2.3", metadata(wheel(), wheel("b" * 64, yanked=True), source)
    ) == ["a" * 64]
    with pytest.raises(ValueError, match="No non-yanked wheels"):
        wheel_hashes("example", "1.2.3", metadata(source, wheel(yanked=True)))


@pytest.mark.parametrize(
    "pin",
    [
        "example>=1.0",
        "-r other.txt",
        "example @ https://example.org/a.whl",
        "--extra-index-url https://example.org",
    ],
)
def test_unpinned_or_external_requirement_sources_are_rejected(pin):
    with pytest.raises(ValueError):
        read_requirements(pin)


def test_changed_version_or_platform_marker_invalidates_lock():
    pins = ['example==1.2.3; sys_platform != "win32"']
    lock = build_lock(pins, lambda name, version: metadata(wheel()))
    for replacement in [
        'example==1.2.4; sys_platform != "win32"',
        'example==1.2.3; sys_platform == "win32"',
    ]:
        with pytest.raises(ValueError, match="changed"):
            validate_lock([replacement], lock)


def test_wrong_release_missing_digest_and_injected_pip_options_fail_closed():
    with pytest.raises(ValueError, match="different"):
        wheel_hashes("other", "1.2.3", metadata(wheel()))
    with pytest.raises(ValueError, match="invalid wheel"):
        wheel_hashes("example", "1.2.3", metadata(wheel("not-a-hash")))
    pins = ["example==1.2.3"]
    lock = build_lock(pins, lambda name, version: metadata(wheel()))
    with pytest.raises(ValueError, match="extra pip option"):
        validate_lock(
            pins,
            lock.replace("--hash=sha256:", "--trusted-host=example.org --hash=sha256:"),
        )
    with pytest.raises(ValueError, match="no wheel hashes"):
        validate_lock(pins, lock.split("example==1.2.3")[0] + "example==1.2.3\n")
