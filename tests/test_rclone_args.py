import pytest

from app.rclone_args import (
    UnsafeRcloneArgument,
    redact_command_text,
    validate_parsed_rclone_args,
    validate_rclone_args,
)


def test_protected_rclone_flags_are_blocked():
    for flag in ("--config=/tmp/evil", "--dry-run=false", "--max-delete=-1", "--rc"):
        with pytest.raises(UnsafeRcloneArgument):
            validate_rclone_args([flag])


def test_tokenized_argument_with_spaces_is_not_split_again():
    args = validate_parsed_rclone_args(["--exclude", "Folder with spaces/**"])
    assert args == ["--exclude", "Folder with spaces/**"]


def test_unsafe_override_is_explicit():
    assert validate_rclone_args(["--config=/tmp/custom"], allow_unsafe=True) == [
        "--config=/tmp/custom"
    ]


def test_rclone_subprocess_env_disables_interactive_prompt(monkeypatch):
    from app.rclone_args import rclone_subprocess_env

    monkeypatch.setenv("RCLONE_ASK_PASSWORD", "true")
    env = rclone_subprocess_env()
    assert env["RCLONE_ASK_PASSWORD"] == "false"


def test_command_text_redacts_flag_and_url_credentials():
    command = (
        "rclone copy --password='canary-one' --token canary-two "
        "--header=Authorization:canary-three https://user:canary-four@example.test/a"
    )

    redacted = redact_command_text(command)

    assert "canary" not in redacted
    assert "--password='***REDACTED***'" in redacted
    assert "--token ***REDACTED***" in redacted
    assert "https://user:***REDACTED***@example.test/a" in redacted
