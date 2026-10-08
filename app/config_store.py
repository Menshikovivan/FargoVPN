"""Serialize config writers across threads and worker processes."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import threading
_LOCK = threading.RLock()
@contextmanager
def config_write_lock(path):
    with _LOCK:
        lock_path = Path(path).with_name(Path(path).name + ".lock")
        with lock_path.open("a+") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
