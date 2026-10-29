"""Changes to native security boundaries must select the macOS CI gate."""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("event", ("push", "pull_request"))
@pytest.mark.parametrize(
    "changed_path",
    (
        "app/webauthn_service.py",
        "app/push_notifications.py",
        "scripts/ios_release_preflight.py",
        "scripts/install_pinned_xcodegen.py",
        "tests/test_ios_security_contract.py",
        "tests/test_native_read_contract.py",
        "tests/test_install_pinned_xcodegen.py",
        "requirements.txt",
        "requirements-dev.txt",
    ),
)
def test_native_security_changes_select_ios_build(event, changed_path):
    workflow = yaml.load(
        (ROOT / ".github/workflows/ios.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    trigger = workflow["on"][event]
    patterns = trigger.get("paths") if isinstance(trigger, dict) else None
    assert patterns is None or any(
        fnmatchcase(changed_path, pattern) for pattern in patterns
    ), f"{event}: {changed_path} would skip the native security gate"
