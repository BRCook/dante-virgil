"""Small file helpers."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file atomically, so a crash never leaves half a file.

    The new file keeps the owner of the file it replaces, or takes the owner
    of its directory if it's new. That way, running virgilctl with sudo
    doesn't leave root-owned files that the daemon can't read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        owner = os.stat(path)
    except FileNotFoundError:
        owner = os.stat(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            try:
                os.fchown(f.fileno(), owner.st_uid, owner.st_gid)
            except PermissionError:
                pass  # Only root can give a file away; anyone else already owns it.
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class UnsafeFileError(Exception):
    pass


def read_small_file(path: Path, limit: int) -> bytes:
    """Read a regular file without following symlinks, refusing anything over the limit."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except PermissionError:
        raise
    except OSError as error:
        raise UnsafeFileError(f"{path}: {error.strerror}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeFileError(f"{path}: not a regular file")
        if info.st_size > limit:
            raise UnsafeFileError(f"{path}: larger than {limit} bytes")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise UnsafeFileError(f"{path}: larger than {limit} bytes")
        return data
    finally:
        os.close(fd)
