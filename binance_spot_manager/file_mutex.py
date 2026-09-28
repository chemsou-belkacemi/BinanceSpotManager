"""Verrou interprocessus libere par le systeme si le processus meurt."""

import os
import time
from pathlib import Path


class FileMutex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self, timeout: float = 0) -> bool:
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        # Windows allows locking a byte range beyond EOF. Do not initialize
        # the file before acquiring: a concurrent owner may already lock byte 0.
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)

    def release(self):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        if not self.acquire(timeout=10):
            raise TimeoutError(f"Verrou occupe : {self.path.name}")
        return self

    def __exit__(self, *args):
        self.release()
