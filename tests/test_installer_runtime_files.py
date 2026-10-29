"""Synthetic filesystem checks for the privileged installer; never run the installer."""

import errno
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.runtime_files import (
    MAX_FILE_BYTES,
    ROLLBACK_FILES,
    RuntimeFiles,
    supported,
)


@pytest.fixture
def application(tmp_path: Path) -> Path:
    if not supported():
        pytest.skip("POSIX no-follow directory descriptors are required")
    root = tmp_path / "app"
    root.mkdir(mode=0o700)
    config = root / "config"
    config.mkdir()
    (config / "config.example.yaml").write_text(
        "web:\n  username: admin\n  password: changeme\nbackup:\n  pairs: []\n",
        encoding="utf-8",
    )
    (config / "rclone-filters.example.txt").write_bytes(b"- *.tmp\n")
    return root


def _runtime(application: Path) -> RuntimeFiles:
    return RuntimeFiles(application, os.geteuid(), os.getegid())


def test_prepare_initialize_and_permissions_use_private_regular_files(application):
    import bcrypt
    import yaml

    with _runtime(application) as runtime:
        runtime.prepare()
        runtime.initialize("dummy-password", "dummy-session-secret")
        runtime.ensure_filters()
        runtime.permissions()
    data = application / "data"
    config = yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))
    assert config["web"]["password"] == ""
    assert bcrypt.checkpw(
        b"dummy-password", config["web"]["password_hash"].encode("ascii")
    )
    assert config["web"]["secret_key"] == "dummy-session-secret"
    assert (data / ".initial-password").read_bytes() == b"dummy-password\n"
    assert (data / "rclone-filters.txt").read_bytes() == b"- *.tmp\n"
    for name in ("config.yaml", ".initial-password", "rclone-filters.txt"):
        info = (data / name).stat()
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.geteuid()
        assert info.st_nlink == 1
    assert not list(application.glob(".runtime-*"))
    assert not list(data.glob(".runtime-stage-*"))


@pytest.mark.parametrize("name", (".initial-password", "rclone-filters.txt"))
def test_root_write_refuses_existing_symlink_and_preserves_target(
    application, tmp_path, name
):
    outside = tmp_path / "outside"
    outside.write_bytes(b"irreplaceable")
    outside.chmod(0o644)
    with _runtime(application) as runtime:
        (application / "data" / name).symlink_to(outside)
        with pytest.raises(OSError):
            runtime.write(name, b"replacement")
    assert outside.read_bytes() == b"irreplaceable"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_missing_filter_does_not_follow_broken_symlink(application, tmp_path):
    outside = tmp_path / "never-created"
    with _runtime(application) as runtime:
        (application / "data" / "rclone-filters.txt").symlink_to(outside)
        with pytest.raises(OSError):
            runtime.ensure_filters()
    assert not outside.exists()


def test_permission_repair_does_not_follow_leaf_symlink(application, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)
    with _runtime(application) as runtime:
        runtime.write("config.yaml", b"web: {}\n")
        runtime.write("rclone-filters.txt", b"")
        (application / "data" / ".initial-password").symlink_to(outside)
        with pytest.raises(OSError):
            runtime.permissions()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("operation", ("write", "permissions", "filters"))
def test_special_file_is_rejected_without_blocking(application, operation):
    with _runtime(application) as runtime:
        name = "rclone-filters.txt" if operation == "filters" else ".initial-password"
        os.mkfifo(application / "data" / name)
        with pytest.raises(ValueError, match="regulär"):
            if operation == "write":
                runtime.write(name, b"new")
            elif operation == "filters":
                runtime.ensure_filters()
            else:
                runtime.permissions()


def test_hardlink_is_rejected_before_privileged_metadata_change(application, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)
    with _runtime(application) as runtime:
        os.link(outside, application / "data" / ".initial-password")
        with pytest.raises(ValueError, match="Hardlinks"):
            runtime.permissions()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_atomic_replacement_does_not_follow_a_late_destination_link(
    application, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    rename = os.rename

    def substitute_then_rename(source, destination, **kwargs):
        (application / "data" / destination).symlink_to(outside)
        return rename(source, destination, **kwargs)

    with _runtime(application) as runtime:
        monkeypatch.setattr(os, "rename", substitute_then_rename)
        runtime.write(".initial-password", b"new-password\n")
    assert outside.read_bytes() == b"outside"
    assert (application / "data" / ".initial-password").is_symlink() is False
    assert (
        application / "data" / ".initial-password"
    ).read_bytes() == b"new-password\n"


def test_atomic_publish_uses_private_destination_filesystem_stage(
    application, monkeypatch
):
    rename = os.rename
    published = []
    with _runtime(application) as runtime:

        def emulate_separate_data_mount(source, destination, **kwargs):
            if kwargs["src_dir_fd"] == runtime.app_fd:
                raise OSError(errno.EXDEV, "dummy separate data filesystem")
            assert kwargs["dst_dir_fd"] == runtime.data_fd
            stage = os.fstat(kwargs["src_dir_fd"])
            assert stage.st_dev == os.fstat(runtime.data_fd).st_dev
            assert stage.st_uid == os.geteuid()
            assert stat.S_IMODE(stage.st_mode) == 0o700
            assert source == "payload"
            published.append(destination)
            return rename(source, destination, **kwargs)

        monkeypatch.setattr(os, "rename", emulate_separate_data_mount)
        runtime.write(".initial-password", b"same-filesystem\n")
    assert published == [".initial-password"]
    data = application / "data"
    assert (data / ".initial-password").read_bytes() == b"same-filesystem\n"
    assert not list(data.glob(".runtime-stage-*"))
    assert not list(application.glob(".runtime-*"))


def test_renamed_private_stage_does_not_redirect_publish_or_cleanup(
    application, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    rename = os.rename
    stage_entry = None
    with _runtime(application) as runtime:

        def replace_stage_name(source, destination, **kwargs):
            nonlocal stage_entry
            stage_entry = next((application / "data").glob(".runtime-stage-*"))
            rename(
                stage_entry.name,
                "moved-stage",
                src_dir_fd=runtime.data_fd,
                dst_dir_fd=runtime.data_fd,
            )
            stage_entry.symlink_to(outside, target_is_directory=True)
            return rename(source, destination, **kwargs)

        monkeypatch.setattr(os, "rename", replace_stage_name)
        runtime.write(".initial-password", b"pinned-stage\n")
    assert (
        application / "data" / ".initial-password"
    ).read_bytes() == b"pinned-stage\n"
    assert list(outside.iterdir()) == []
    assert stage_entry.is_symlink()
    assert list((application / "data" / "moved-stage").iterdir()) == []


@pytest.mark.parametrize("substitution", ["owner", "permissions"])
def test_untrusted_private_stage_is_rejected_before_payload_creation(
    application, monkeypatch, substitution
):
    original_open = os.open
    original_stat = os.fstat
    stage_descriptor = None
    with _runtime(application) as runtime:

        def open_stage(name, flags, **kwargs):
            nonlocal stage_descriptor
            descriptor = original_open(name, flags, **kwargs)
            if str(name).startswith(".runtime-stage-"):
                stage_descriptor = descriptor
                if substitution == "permissions":
                    os.fchmod(descriptor, 0o770)
            return descriptor

        def inspect_stage(descriptor):
            info = original_stat(descriptor)
            if descriptor == stage_descriptor and substitution == "owner":
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_uid=info.st_uid + 1,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                )
            return info

        monkeypatch.setattr(os, "open", open_stage)
        monkeypatch.setattr(os, "fstat", inspect_stage)
        with pytest.raises(ValueError, match="privat"):
            runtime.write(".initial-password", b"must-not-publish")
    assert not (application / "data" / ".initial-password").exists()
    assert not list((application / "data").glob(".runtime-stage-*/payload"))


def test_pinned_data_handle_cannot_be_redirected_after_open(application, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with _runtime(application) as runtime:
        (application / "data").rename(application / "original-data")
        (application / "data").symlink_to(outside, target_is_directory=True)
        runtime.write(".initial-password", b"confined\n")
    assert not (outside / ".initial-password").exists()
    assert (
        application / "original-data" / ".initial-password"
    ).read_bytes() == b"confined\n"


def test_data_directory_symlink_is_rejected_before_any_runtime_write(
    application, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (application / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        with _runtime(application):
            pytest.fail("A symlinked data root must never be entered")
    assert list(outside.iterdir()) == []


def test_group_writable_application_root_is_rejected(application):
    application.chmod(0o770)
    with pytest.raises(ValueError, match="APP_DIR"):
        with _runtime(application):
            pytest.fail("An untrusted application root must never be entered")
    assert not (application / "data").exists()


def test_helper_is_required_before_privileged_runtime_leaf_operations():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install.sh").read_text(
        encoding="utf-8"
    )
    assert 'RUNTIME_HELPER="$ROLLBACK_RUNTIME_HELPER"' in script
    assert "RUNTIME_HELPER_DIR=$(mktemp -d /run/rclone-sync-runtime.XXXXXX)" in script
    pin = script.index(
        '"$SOURCE_COMMIT:scripts/runtime_files.py" > "$ROLLBACK_RUNTIME_HELPER"'
    )
    assert script.index("merge-base --is-ancestor") < pin
    assert pin < script.index('checkout --detach "$SOURCE_COMMIT"')
    assert '/usr/bin/python3 -I "$ROLLBACK_RUNTIME_HELPER"' in script
    assert (
        '"$APP_DIR_CANONICAL" "$RUNTIME_UID" "$RUNTIME_GID" restore "$restore_dir"'
        in script
    )
    assert script.index("restore_runtime_backup || rollback_failed=1") < script.index(
        'rm -rf -- "$RUNTIME_HELPER_DIR"'
    )
    assert '"$APP_DIR" "$RUNTIME_UID" "$RUNTIME_GID" initialize' in script
    assert '"$APP_DIR" "$RUNTIME_UID" "$RUNTIME_GID" permissions' in script
    assert '> "$APP_DIR/data/.initial-password"' not in script
    assert 'cp "$APP_DIR/config/rclone-filters.example.txt"' not in script
    assert 'chmod 0600 "$APP_DIR/data/' not in script
    assert 'chown -hR "$APP_USER:$APP_GROUP"' not in script
    assert "-xdev \\( -type d -o -type f -links 1 \\)" in script


@pytest.mark.parametrize("late_entry", ["regular", "symlink"])
def test_require_missing_never_overwrites_a_late_created_config(
    application, tmp_path, monkeypatch, late_entry
):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    link = os.link
    destination = application / "data" / "config.yaml"
    with _runtime(application) as runtime:

        def create_then_link(source, name, **kwargs):
            assert kwargs["dst_dir_fd"] == runtime.data_fd
            assert (
                os.fstat(kwargs["src_dir_fd"]).st_dev
                == os.fstat(runtime.data_fd).st_dev
            )
            if late_entry == "regular":
                destination.write_bytes(b"late-owner-config")
            else:
                destination.symlink_to(outside)
            return link(source, name, **kwargs)

        monkeypatch.setattr(os, "link", create_then_link)
        with pytest.raises(FileExistsError):
            runtime.write("config.yaml", b"must-not-overwrite", require_missing=True)
    assert outside.read_bytes() == b"outside"
    if late_entry == "regular":
        assert destination.read_bytes() == b"late-owner-config"
    else:
        assert destination.is_symlink()
    assert not list((application / "data").glob(".runtime-stage-*"))


def _rollback_backup(
    application: Path, tmp_path: Path, *, config=b"saved-config"
) -> Path:
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    (backup / "data").mkdir(mode=0o700)
    (backup / "runtime-state").mkdir(mode=0o700)
    (backup / ".rclone-sync-backup-v1").write_bytes(b"rclone-sync-backup-v1\n")
    (backup / "source-path.txt").write_text(str(application) + "\n", encoding="utf-8")
    for name in ROLLBACK_FILES:
        present = name == "config.yaml" and config is not None
        suffix = ".present" if present else ".missing"
        (backup / "runtime-state" / (name + suffix)).touch()
        if present:
            (backup / "data" / name).write_bytes(config)
    return backup


def test_rollback_refuses_a_late_backup_source_symlink(
    application, tmp_path, monkeypatch
):
    backup = _rollback_backup(application, tmp_path)
    source = backup / "data" / "config.yaml"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-secret")
    backup_data_inode = (backup / "data").stat().st_ino
    open_file = os.open
    with _runtime(application) as runtime:
        runtime.write("config.yaml", b"current-config")

        def replace_source(name, flags, **kwargs):
            parent = kwargs.get("dir_fd")
            if (
                name == "config.yaml"
                and parent is not None
                and os.fstat(parent).st_ino == backup_data_inode
            ):
                source.unlink()
                source.symlink_to(outside)
            return open_file(name, flags, **kwargs)

        monkeypatch.setattr(os, "open", replace_source)
        with pytest.raises(OSError):
            runtime.restore(backup)
    assert outside.read_bytes() == b"outside-secret"
    assert (application / "data" / "config.yaml").read_bytes() == b"current-config"


def test_rollback_private_stage_replaces_a_late_destination_link_safely(
    application, tmp_path, monkeypatch
):
    backup = _rollback_backup(application, tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-secret")
    rename = os.rename
    destination = application / "data" / "config.yaml"
    with _runtime(application) as runtime:

        def replace_destination(source, name, **kwargs):
            stage = os.fstat(kwargs["src_dir_fd"])
            assert source == "payload"
            assert kwargs["src_dir_fd"] != runtime.data_fd
            assert stage.st_uid == os.geteuid()
            assert stat.S_IMODE(stage.st_mode) == 0o700
            destination.symlink_to(outside)
            return rename(source, name, **kwargs)

        monkeypatch.setattr(os, "rename", replace_destination)
        runtime.restore(backup)
    assert outside.read_bytes() == b"outside-secret"
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"saved-config"
    assert not list((application / "data").glob(".rollback-*"))


def test_rollback_streams_a_database_larger_than_the_config_limit(
    application, tmp_path, monkeypatch
):
    backup = _rollback_backup(application, tmp_path)
    source = backup / "data" / "rclone-sync.db"
    payload = b"x" * (MAX_FILE_BYTES * 2 + 513)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE fixture(value BLOB)")
        connection.execute("INSERT INTO fixture VALUES (?)", (payload,))
    (backup / "runtime-state" / "rclone-sync.db.missing").unlink()
    (backup / "runtime-state" / "rclone-sync.db.present").touch()
    assert source.stat().st_size > MAX_FILE_BYTES
    read = os.read
    requests = []
    with _runtime(application) as runtime:

        def bounded_read(descriptor, size):
            assert size <= MAX_FILE_BYTES
            requests.append(size)
            return read(descriptor, size)

        monkeypatch.setattr(os, "read", bounded_read)
        runtime.restore(backup)
    with sqlite3.connect(application / "data" / "rclone-sync.db") as connection:
        assert connection.execute("SELECT value FROM fixture").fetchone()[0] == payload
    assert requests.count(MAX_FILE_BYTES) >= 2


def test_rollback_unlinks_only_pinned_runtime_entries_after_data_path_swap(
    application, tmp_path
):
    backup = _rollback_backup(application, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected"
    protected.write_bytes(b"must-remain")
    (outside / "config.yaml").write_bytes(b"outside-config")
    with _runtime(application) as runtime:
        original = application / "original-data"
        (application / "data").rename(original)
        (application / "data").symlink_to(outside, target_is_directory=True)
        for name in (
            "config.yaml.bak",
            "rclone-sync.db",
            "rclone-sync.db-wal",
            "rclone-sync.db-shm",
        ):
            (original / name).symlink_to(protected)
        runtime.restore(backup)
    assert (original / "config.yaml").read_bytes() == b"saved-config"
    for name in (
        "config.yaml.bak",
        "rclone-sync.db",
        "rclone-sync.db-wal",
        "rclone-sync.db-shm",
    ):
        assert not (original / name).is_symlink()
        assert not (original / name).exists()
    assert protected.read_bytes() == b"must-remain"
    assert (outside / "config.yaml").read_bytes() == b"outside-config"


def test_rollback_rejects_a_corrupt_database_before_its_publication(
    application, tmp_path
):
    backup = _rollback_backup(application, tmp_path)
    (backup / "data" / "rclone-sync.db").write_bytes(b"not-a-sqlite-database")
    (backup / "runtime-state" / "rclone-sync.db.missing").unlink()
    (backup / "runtime-state" / "rclone-sync.db.present").touch()
    with _runtime(application) as runtime:
        with pytest.raises(sqlite3.DatabaseError):
            runtime.restore(backup)
    assert not (application / "data" / "rclone-sync.db").exists()
    assert not list((application / "data").glob(".runtime-stage-*"))


def test_rollback_removes_new_runtime_files_when_no_old_data_directory_existed(
    application, tmp_path
):
    backup = _rollback_backup(application, tmp_path, config=None)
    (backup / "data").rmdir()
    with _runtime(application) as runtime:
        runtime.write("config.yaml", b"new-config")
        runtime.restore(backup)
    assert not (application / "data" / "config.yaml").exists()
