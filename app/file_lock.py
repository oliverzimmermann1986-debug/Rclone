"""Small cross-platform advisory file-lock adapter.

Production Linux uses ``flock`` with its full shared/exclusive semantics. Windows
uses an exclusive one-byte ``msvcrt`` lock so development and tests can exercise
the same critical sections without weakening the Linux runtime behavior.
"""

from __future__ import annotations

import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire(fd: int, *, exclusive: bool = True, blocking: bool = True) -> None:
    if os.name != "nt":
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(fd, operation)
        return

    # msvcrt locks bytes from the current file position and cannot lock an empty
    # file. Keep one private marker byte and always lock byte zero.
    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    try:
        msvcrt.locking(fd, mode, 1)
    except OSError as exc:
        if not blocking:
            raise BlockingIOError(str(exc)) from exc
        raise


def release(fd: int) -> None:
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
