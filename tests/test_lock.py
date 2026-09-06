# tests/test_lock.py
import os
import time

import pytest

from ifwi_mcp import lock


def test_acquire_release_allows_reacquire(clean_env):
    with lock.acquire("key-a"):
        pass
    with lock.acquire("key-a"):
        pass  # would hang/timeout if the first release didn't clean up the lock file


def test_acquire_is_exclusive_for_the_duration_of_the_block(clean_env):
    path = lock._lock_path("key-b")
    with lock.acquire("key-b"):
        assert path.is_file()
    assert not path.is_file()


def test_different_keys_do_not_contend(clean_env):
    with lock.acquire("key-c"):
        with lock.acquire("key-d"):
            pass  # a different key must not block on key-c's held lock


def test_acquire_times_out_when_already_held(clean_env):
    path = lock._lock_path("key-e")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        with pytest.raises(TimeoutError):
            with lock.acquire("key-e", timeout=0.5):
                pass
    finally:
        path.unlink(missing_ok=True)


def test_stale_lock_is_stolen(clean_env):
    path = lock._lock_path("key-f")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    old = time.time() - 7200
    os.utime(path, (old, old))
    with lock.acquire("key-f", timeout=1):
        pass  # the stale (2h old) lock must be stolen rather than timing out
