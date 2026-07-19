from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold one byte of an installer-owned lock file across supported hosts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            windows_lock: Any = importlib.import_module("msvcrt")

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            windows_lock.locking(stream.fileno(), windows_lock.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                windows_lock.locking(stream.fileno(), windows_lock.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
