"""Keep the laptop from sleeping while the platform has work to do (trading days, `service.awake_from`
to `service.awake_until`). Windows: SetThreadExecutionState from a thread that holds the request;
macOS: `caffeinate -i -w <pid>`; Linux: `systemd-inhibit ... sleep infinity`. Closing a laptop's lid
can still force sleep: see the README for the power settings."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional

log = logging.getLogger(__name__)


class NullBackend:
    name = "none"

    def hold(self) -> bool:
        return False

    def release(self) -> None:
        pass


class WindowsBackend:
    name = "windows"
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self):
        self._stop: Optional[threading.Event] = None

    def hold(self) -> bool:
        import ctypes

        self._stop = threading.Event()
        stop = self._stop

        def run():
            k = ctypes.windll.kernel32
            k.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            stop.wait()
            k.SetThreadExecutionState(self.ES_CONTINUOUS)

        threading.Thread(target=run, name="keep-awake", daemon=True).start()
        return True

    def release(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._stop = None


class ProcessBackend:
    """Holds a helper process (caffeinate or systemd-inhibit) while awake is wanted."""

    def __init__(self, name: str, argv):
        self.name, self.argv = name, argv
        self.proc: Optional[subprocess.Popen] = None

    def hold(self) -> bool:
        if not shutil.which(self.argv[0]):
            log.warning("keep-awake: %s not found; the laptop may sleep", self.argv[0])
            return False
        self.proc = subprocess.Popen(self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def release(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def default_backend():
    if sys.platform.startswith("win"):
        return WindowsBackend()
    if sys.platform == "darwin":
        return ProcessBackend("caffeinate", ["caffeinate", "-i", "-w", str(os.getpid())])
    if sys.platform.startswith("linux"):
        return ProcessBackend("systemd-inhibit", ["systemd-inhibit", "--what=idle:sleep", "--who=credit-spreads",
                                                  "--why=market hours", "--mode=block", "sleep", "infinity"])
    return NullBackend()


class KeepAwake:
    def __init__(self, backend=None):
        self.backend = backend or default_backend()
        self.held = False

    def want(self, awake: bool) -> None:
        if awake and not self.held:
            self.held = bool(self.backend.hold())
            if self.held:
                log.info("keep-awake on (%s)", self.backend.name)
        elif not awake and self.held:
            self.backend.release()
            self.held = False
            log.info("keep-awake off")
