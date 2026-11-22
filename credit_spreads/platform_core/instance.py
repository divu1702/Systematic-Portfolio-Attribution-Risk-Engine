"""One platform process at a time: two would trade every account twice. The lock is an operating-system
file lock, released automatically if the process dies."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


class SingleInstance:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def holder(self) -> Optional[str]:
        try:
            return self.path.read_text().strip() or None
        except OSError:
            return None

    def release(self) -> None:
        if self._fh is not None:
            try:
                if sys.platform.startswith("win"):
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
