"""Cross-process advisory locking for populating shared cache entries.

Built on O_CREAT|O_EXCL, which is an atomic "create if absent" on both Windows and
POSIX -- no extra dependency needed. One lock file per key, named by its hash so
arbitrary strings (paths) are safe filenames.
"""
import contextlib
import hashlib
import os
import time

from . import config

_STALE_SECONDS = 3600
_POLL_SECONDS = 0.2


def _lock_path(key: str):
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    return config.cache_subdir(".locks") / f"{digest}.lock"


@contextlib.contextmanager
def acquire(key: str, timeout: float = 900):
    """Hold an exclusive lock for `key` for the life of the with-block.

    Raises TimeoutError if it cannot be acquired within `timeout` seconds. A lock
    file older than _STALE_SECONDS is assumed to be left by a crashed holder and
    is stolen.
    """
    path = _lock_path(key)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue  # released between the failed create and this stat
            if age > _STALE_SECONDS:
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {key}")
            time.sleep(_POLL_SECONDS)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
