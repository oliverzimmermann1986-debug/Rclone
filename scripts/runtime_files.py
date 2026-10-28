"""Confine privileged installer file operations to opened runtime directories."""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import sqlite3
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

MAX_FILE_BYTES = 1024 * 1024
RUNTIME_FILES = frozenset(
    {"config.yaml", "config.yaml.bak", ".initial-password", "rclone-filters.txt"}
)
ROLLBACK_FILES = ("config.yaml", "config.yaml.bak", "rclone-sync.db")
DATABASE_SIDECARS = ("rclone-sync.db-wal", "rclone-sync.db-shm")


def supported() -> bool:
    return all(
        hasattr(os, name)
        for name in (
            "O_DIRECTORY",
            "O_NOFOLLOW",
            "O_NONBLOCK",
            "O_CLOEXEC",
            "fchmod",
            "fchown",
        )
    ) and all(
        function in os.supports_dir_fd
        for function in (
            os.open,
            os.mkdir,
            os.rename,
            os.link,
            os.unlink,
            os.stat,
            os.rmdir,
        )
    )


class RuntimeFiles:
    """Use trusted parent handles; never reopen a writable path by its full name."""

    def __init__(self, application: Path, uid: int, gid: int):
        if uid < 0 or gid < 0:
            raise ValueError("Runtime-Besitzer und Gruppe müssen gültig sein")
        self.application = Path(application)
        self.uid = uid
        self.gid = gid
        self.app_fd: int | None = None
        self.data_fd: int | None = None

    def __enter__(self) -> RuntimeFiles:
        if (
            not supported()
            or not self.application.is_absolute()
            or self.application == Path(self.application.anchor)
        ):
            raise ValueError(
                "Sichere absolute Runtime-Dateizugriffe sind nicht verfügbar"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(self.application.anchor, flags)
        try:
            for component in self.application.parts[1:]:
                following = os.open(component, flags, dir_fd=current)
                os.close(current)
                current = following
            info = os.fstat(current)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError(
                    "APP_DIR muss dem Installer gehören und schreibgeschützt sein"
                )
            self.app_fd = current
            current = -1
            self.data_fd = self._directory(self.app_fd, "data")
        except BaseException:
            if current >= 0:
                os.close(current)
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_args) -> None:
        for name in ("data_fd", "app_fd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    @staticmethod
    def _directory(parent: int, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )

    @staticmethod
    def _regular(descriptor: int) -> os.stat_result:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(
                "Runtime-Datei muss regulär und ohne weitere Hardlinks sein"
            )
        return info

    def _require_open(self) -> None:
        if self.app_fd is None or self.data_fd is None:
            raise ValueError("Runtime-Verzeichnisse sind nicht geöffnet")

    def _open_file(self, name: str, *, missing_ok: bool = False) -> int | None:
        self._require_open()
        if name not in RUNTIME_FILES:
            raise ValueError("Unbekannte Runtime-Datei")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.data_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        try:
            self._regular(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def prepare(self) -> None:
        self._require_open()
        for parent, names in (
            (self.app_fd, ("data", "logs", "temp")),
            (self.data_fd, (".rclone-cache", "runtime")),
        ):
            for name in names:
                descriptor = self._directory(parent, name)
                try:
                    os.fchmod(descriptor, 0o700)
                    os.fchown(descriptor, self.uid, self.gid)
                finally:
                    os.close(descriptor)

    @contextmanager
    def _private_stage(self) -> Iterator[int]:
        """Pin a private installer-owned directory on the destination filesystem."""
        self._require_open()
        name = ".runtime-stage-" + secrets.token_hex(16)
        os.mkdir(name, 0o700, dir_fd=self.data_fd)
        descriptor = None
        trusted = False
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self.data_fd,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_dev != os.fstat(self.data_fd).st_dev
            ):
                raise ValueError(
                    "Temporäre Runtime-Ablage muss dem Installer gehören und privat sein"
                )
            trusted = True
            yield descriptor
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                if trusted:
                    # A service user may rename the directory entry but cannot
                    # access this directory's contents. Never recurse or remove
                    # an entry that no longer denotes the pinned stage.
                    current = os.stat(name, dir_fd=self.data_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                        os.rmdir(name, dir_fd=self.data_fd)
            except OSError as exc:
                if not failed and exc.errno not in {
                    errno.ENOENT,
                    errno.ENOTDIR,
                    errno.ENOTEMPTY,
                }:
                    raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _publish(
        self,
        name: str,
        chunks: Iterable[bytes],
        *,
        require_missing: bool = False,
        database: bool = False,
    ) -> None:
        with self._private_stage() as stage:
            temporary = "payload"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=stage,
            )
            try:
                self._regular(descriptor)
                for chunk in chunks:
                    remaining = memoryview(chunk)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError(
                                "Runtime-Datei konnte nicht geschrieben werden"
                            )
                        remaining = remaining[written:]
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, self.uid, self.gid)
                os.fsync(descriptor)
                self._regular(descriptor)
                if database:
                    self._validate_database(stage)
                # Even after the leaf becomes service-owned, the service user
                # cannot traverse the installer-owned private source directory.
                if require_missing:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=stage,
                        dst_dir_fd=self.data_fd,
                        follow_symlinks=False,
                    )
                    os.unlink(temporary, dir_fd=stage)
                else:
                    os.rename(
                        temporary, name, src_dir_fd=stage, dst_dir_fd=self.data_fd
                    )
                os.fsync(self.data_fd)
                os.fsync(stage)
            finally:
                os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=stage)
                except FileNotFoundError:
                    pass

    def write(
        self, name: str, payload: bytes, *, require_missing: bool = False
    ) -> None:
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError("Runtime-Datei überschreitet 1 MiB")
        previous = self._open_file(name, missing_ok=True)
        if previous is not None:
            os.close(previous)
            if require_missing:
                raise FileExistsError(name)
        self._publish(name, (payload,), require_missing=require_missing)

    @staticmethod
    def _validate_database(stage: int) -> None:
        # Linux installer: the descriptor pins the private stage even if its
        # public directory entry is renamed. SQLite never opens a runtime path.
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{stage}/payload?mode=ro&immutable=1", uri=True
        )
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite-Rollback-Sicherung ist inkonsistent")
        finally:
            connection.close()

    @classmethod
    def _open_regular_at(
        cls, parent: int, name: str, *, missing_ok: bool = False
    ) -> int | None:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        try:
            cls._regular(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _private_directory(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise ValueError(
                "Rollback-Sicherung muss dem Installer gehören und privat sein"
            )

    @contextmanager
    def _backup_directory(self, backup: Path) -> Iterator[int]:
        backup = Path(backup)
        if not backup.is_absolute() or ".." in backup.parts:
            raise ValueError(
                "Rollback-Sicherung muss einen sicheren absoluten Pfad haben"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(backup.anchor, flags)
        try:
            for component in backup.parts[1:]:
                following = os.open(component, flags, dir_fd=current)
                os.close(current)
                current = following
            self._private_directory(current)
            yield current
        finally:
            os.close(current)

    @classmethod
    def _stream_file(cls, descriptor: int) -> Iterator[bytes]:
        before = cls._regular(descriptor)
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(MAX_FILE_BYTES, remaining))
            if not chunk:
                raise ValueError("Rollback-Datei wurde während des Lesens verkürzt")
            remaining -= len(chunk)
            yield chunk
        after = cls._regular(descriptor)
        if os.read(descriptor, 1) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Rollback-Datei wurde während des Lesens geändert")

    def unlink(self, name: str) -> None:
        self._require_open()
        if name not in (*ROLLBACK_FILES, *DATABASE_SIDECARS):
            raise ValueError("Unbekannte Rollback-Datei")
        try:
            os.unlink(name, dir_fd=self.data_fd)
        except FileNotFoundError:
            pass
        os.fsync(self.data_fd)

    def restore(self, backup: Path) -> None:
        """Restore only pinned regular backup files; never use writable leaf paths."""
        self._require_open()
        descriptors: list[tuple[str, int | None]] = []
        with self._backup_directory(backup) as backup_fd:
            data = state = None
            try:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                try:
                    data = os.open("data", flags, dir_fd=backup_fd)
                except FileNotFoundError:
                    pass
                state = os.open("runtime-state", flags, dir_fd=backup_fd)
                if data is not None:
                    self._private_directory(data)
                self._private_directory(state)
                for name, expected in (
                    (".rclone-sync-backup-v1", b"rclone-sync-backup-v1\n"),
                    ("source-path.txt", (str(self.application) + "\n").encode("utf-8")),
                ):
                    marker = self._open_regular_at(backup_fd, name)
                    try:
                        if os.read(marker, len(expected) + 1) != expected:
                            raise ValueError(
                                "Rollback-Sicherung gehört nicht zu dieser Installation"
                            )
                    finally:
                        os.close(marker)
                for name in ROLLBACK_FILES:
                    present = self._open_regular_at(
                        state, name + ".present", missing_ok=True
                    )
                    missing = None
                    try:
                        missing = self._open_regular_at(
                            state, name + ".missing", missing_ok=True
                        )
                        if (present is None) == (missing is None):
                            raise ValueError(
                                "Rollback-Dateizustand ist nicht eindeutig"
                            )
                        descriptor = None
                        if present is not None:
                            if data is None:
                                raise ValueError("Rollback-Datenverzeichnis fehlt")
                            descriptor = self._open_regular_at(data, name)
                        descriptors.append((name, descriptor))
                    finally:
                        for marker in (present, missing):
                            if marker is not None:
                                os.close(marker)
                for name in DATABASE_SIDECARS:
                    self.unlink(name)
                for name, descriptor in descriptors:
                    if descriptor is None:
                        self.unlink(name)
                    else:
                        self._publish(
                            name,
                            self._stream_file(descriptor),
                            database=name == "rclone-sync.db",
                        )
            finally:
                for _name, descriptor in descriptors:
                    if descriptor is not None:
                        os.close(descriptor)
                for descriptor in (state, data):
                    if descriptor is not None:
                        os.close(descriptor)

    def template(self, name: str) -> bytes:
        self._require_open()
        if name not in {"config.example.yaml", "rclone-filters.example.txt"}:
            raise ValueError("Unbekannte Runtime-Vorlage")
        directory = os.open(
            "config",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.app_fd,
        )
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        try:
            info = self._regular(descriptor)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError("Runtime-Vorlage ist nicht vertrauenswürdig")
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError("Runtime-Vorlage überschreitet 1 MiB")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                payload = source.read(MAX_FILE_BYTES + 1)
            if len(payload) > MAX_FILE_BYTES:
                raise ValueError("Runtime-Vorlage überschreitet 1 MiB")
            return payload
        finally:
            os.close(descriptor)

    def ensure_filters(self) -> None:
        existing = self._open_file("rclone-filters.txt", missing_ok=True)
        if existing is not None:
            os.close(existing)
            return
        self.write(
            "rclone-filters.txt",
            self.template("rclone-filters.example.txt"),
            require_missing=True,
        )

    def initialize(self, password: str, secret: str) -> None:
        import bcrypt
        import yaml

        config = yaml.safe_load(self.template("config.example.yaml"))
        if not isinstance(config, dict) or not isinstance(config.get("web"), dict):
            raise ValueError("Konfigurationsvorlage muss ein web-Mapping enthalten")
        web = config["web"]
        web["password"] = ""
        web["password_hash"] = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("ascii")
        web["secret_key"] = secret
        web["session_version"] = 1
        self.write(
            "config.yaml",
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"),
            require_missing=True,
        )
        self.write(".initial-password", (password + "\n").encode("utf-8"))

    def permissions(self) -> None:
        for name in sorted(RUNTIME_FILES):
            descriptor = self._open_file(
                name, missing_ok=name in {"config.yaml.bak", ".initial-password"}
            )
            if descriptor is None:
                continue
            try:
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, self.uid, self.gid)
            finally:
                os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application", type=Path)
    parser.add_argument("uid", type=int)
    parser.add_argument("gid", type=int)
    parser.add_argument(
        "operation",
        choices=("prepare", "initialize", "filters", "permissions", "restore"),
    )
    parser.add_argument("backup", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        with RuntimeFiles(args.application, args.uid, args.gid) as runtime:
            if args.operation == "initialize":
                password = os.environ["INITIAL_PASSWORD"]
                secret = os.environ["GEN_SECRET"]
                if not password or not secret:
                    raise ValueError("Initiale Zugangsdaten fehlen")
                runtime.initialize(password, secret)
            elif args.operation == "filters":
                runtime.ensure_filters()
            elif args.operation == "restore":
                if args.backup is None:
                    raise ValueError("Rollback-Sicherung fehlt")
                runtime.restore(args.backup)
            else:
                getattr(runtime, args.operation)()
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"Sicherer Runtime-Dateizugriff fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
