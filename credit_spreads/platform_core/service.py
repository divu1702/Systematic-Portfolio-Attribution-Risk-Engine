"""Start the platform with the laptop and keep it running, without anyone at the keyboard.

`python -m platform_core service install` registers `python -m platform_core run` with the operating
system, using the Python it is run with (run it from the project's virtual environment):

- Windows: a Task Scheduler task that starts at log-on, restarts within a minute if the process
  stops, re-checks every 15 minutes, runs on battery, has no time limit, and wakes the laptop at 08:30
  on weekdays when wake timers are allowed.
- macOS: a launchd agent (RunAtLoad, KeepAlive) in ~/Library/LaunchAgents.
- Linux: a systemd user service (Restart=always), with lingering so it starts at boot.

Then `service status | start | stop | restart | logs | uninstall`. Only one platform process can
run at a time (platform_core/instance.py), so a manual `run` next to the service does no harm.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from xml.sax.saxutils import escape

from .config import PROJECT_ROOT

TASK = "CreditSpreadsPlatform"
LABEL = "com.creditspreads.platform"
UNIT = "credit-spreads.service"
DESCRIPTION = "Credit spreads platform: paper trading and the market-data repository"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


def os_name(platform: Optional[str] = None) -> str:
    p = platform or sys.platform
    return "windows" if p.startswith("win") else "macos" if p == "darwin" else "linux"


def python_for(kind: str, python: Optional[str] = None) -> str:
    py = Path(python or sys.executable)
    if kind == "windows":
        w = py.with_name("pythonw.exe")                 # no console window
        return str(w if w.exists() else py)
    return str(py)


def args_for(config: Path) -> List[str]:
    return ["-m", "platform_core", "--config", str(config), "run"]


# ---------------------------------------------------------------------- unit files
def systemd_unit(python: str, project: Path, config: Path) -> str:
    cmd = " ".join([python] + [f'"{a}"' if " " in a else a for a in args_for(config)])
    return (f"[Unit]\nDescription={DESCRIPTION}\n\n[Service]\nType=simple\nWorkingDirectory={project}\n"
            f"ExecStart={cmd}\nRestart=always\nRestartSec=30\nEnvironment=PYTHONUNBUFFERED=1\n\n"
            f"[Install]\nWantedBy=default.target\n")


def launchd_plist(python: str, project: Path, config: Path, logs: Path) -> bytes:
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [python] + args_for(config), "WorkingDirectory": str(project),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "service.out.log"), "StandardErrorPath": str(logs / "service.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"}})


def windows_task_xml(python: str, project: Path, config: Path, user: str) -> str:
    args = " ".join(f'"{a}"' if " " in a else a for a in args_for(config))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(DESCRIPTION)}</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </LogonTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T08:30:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Monday /><Tuesday /><Wednesday /><Thursday /><Friday /></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(project))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# ---------------------------------------------------------------------- per-OS actions
class Service:
    def __init__(self, config: Path, project: Path = PROJECT_ROOT, python: Optional[str] = None,
                 platform: Optional[str] = None, run: Runner = _run, home: Optional[Path] = None):
        self.kind = os_name(platform)
        self.config, self.project = Path(config).resolve(), Path(project)
        self.python = python_for(self.kind, python)
        self.run, self.home = run, Path(home or Path.home())
        self.logs = self.project / "logs"

    # paths
    @property
    def unit_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / UNIT

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def xml_path(self) -> Path:
        return self.project / "data" / f"{TASK}.xml"

    def _uid(self) -> str:
        return str(os.getuid()) if hasattr(os, "getuid") else "0"

    def _do(self, cmd: Sequence[str], out: List[str], ok_codes=(0,)) -> bool:
        r = self.run(cmd)
        line = " ".join(cmd)
        if r.returncode in ok_codes:
            out.append(f"ok: {line}")
            return True
        out.append(f"failed ({r.returncode}): {line}: {(r.stderr or r.stdout or '').strip()[:300]}")
        return False

    def install(self) -> List[str]:
        out: List[str] = []
        self.logs.mkdir(parents=True, exist_ok=True)
        if self.kind == "linux":
            self.unit_path.parent.mkdir(parents=True, exist_ok=True)
            self.unit_path.write_text(systemd_unit(self.python, self.project, self.config))
            out.append(f"wrote {self.unit_path}")
            self._do(["systemctl", "--user", "daemon-reload"], out)
            self._do(["systemctl", "--user", "enable", "--now", UNIT], out)
            if not self._do(["loginctl", "enable-linger", getpass.getuser()], out):
                out.append("To start at boot before you log in, run: sudo loginctl enable-linger " + getpass.getuser())
        elif self.kind == "macos":
            self.plist_path.parent.mkdir(parents=True, exist_ok=True)
            self.plist_path.write_bytes(launchd_plist(self.python, self.project, self.config, self.logs))
            out.append(f"wrote {self.plist_path}")
            self.run(["launchctl", "bootout", f"gui/{self._uid()}/{LABEL}"])          # replace an older copy
            self._do(["launchctl", "bootstrap", f"gui/{self._uid()}", str(self.plist_path)], out)
            self._do(["launchctl", "enable", f"gui/{self._uid()}/{LABEL}"], out)
            out.append("Optional, to wake the Mac on weekday mornings: sudo pmset repeat wakeorpoweron MTWRF 08:30:00")
        else:
            user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', getpass.getuser())}".lstrip("\\")
            self.xml_path.parent.mkdir(parents=True, exist_ok=True)
            self.xml_path.write_text(windows_task_xml(self.python, self.project, self.config, user), encoding="utf-16")
            out.append(f"wrote {self.xml_path}")
            self._do(["schtasks", "/Create", "/TN", TASK, "/XML", str(self.xml_path), "/F"], out)
            self._do(["schtasks", "/Run", "/TN", TASK], out)
        return out

    def uninstall(self) -> List[str]:
        out: List[str] = []
        if self.kind == "linux":
            self._do(["systemctl", "--user", "disable", "--now", UNIT], out)
            if self.unit_path.exists():
                self.unit_path.unlink()
                out.append(f"removed {self.unit_path}")
            self._do(["systemctl", "--user", "daemon-reload"], out)
        elif self.kind == "macos":
            self._do(["launchctl", "bootout", f"gui/{self._uid()}/{LABEL}"], out)
            if self.plist_path.exists():
                self.plist_path.unlink()
                out.append(f"removed {self.plist_path}")
        else:
            self._do(["schtasks", "/End", "/TN", TASK], out, ok_codes=(0, 1))
            self._do(["schtasks", "/Delete", "/TN", TASK, "/F"], out)
        return out

    def start(self) -> List[str]:
        out: List[str] = []
        if self.kind == "linux":
            self._do(["systemctl", "--user", "start", UNIT], out)
        elif self.kind == "macos":
            if not self._do(["launchctl", "kickstart", f"gui/{self._uid()}/{LABEL}"], out):
                self._do(["launchctl", "bootstrap", f"gui/{self._uid()}", str(self.plist_path)], out)
        else:
            self._do(["schtasks", "/Change", "/TN", TASK, "/ENABLE"], out)
            self._do(["schtasks", "/Run", "/TN", TASK], out)
        return out

    def stop(self) -> List[str]:
        """Stops it until the next `service start` (or, on Windows and macOS, the next log-in)."""
        out: List[str] = []
        if self.kind == "linux":
            self._do(["systemctl", "--user", "stop", UNIT], out)
        elif self.kind == "macos":
            self._do(["launchctl", "bootout", f"gui/{self._uid()}/{LABEL}"], out)
        else:
            self._do(["schtasks", "/Change", "/TN", TASK, "/DISABLE"], out)   # or the 15-minute check restarts it
            self._do(["schtasks", "/End", "/TN", TASK], out, ok_codes=(0, 1))
            out.append(f"Disabled until `python -m platform_core service start`.")
        return out

    def restart(self) -> List[str]:
        if self.kind == "linux":
            out: List[str] = []
            self._do(["systemctl", "--user", "restart", UNIT], out)
            return out
        if self.kind == "macos":
            out = []
            self._do(["launchctl", "kickstart", "-k", f"gui/{self._uid()}/{LABEL}"], out)
            return out
        return self.stop() + self.start()

    def status(self) -> str:
        if self.kind == "linux":
            r = self.run(["systemctl", "--user", "status", UNIT, "--no-pager", "--lines=0"])
        elif self.kind == "macos":
            r = self.run(["launchctl", "print", f"gui/{self._uid()}/{LABEL}"])
        else:
            r = self.run(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"])
        text = (r.stdout or r.stderr or "").strip()
        return text if r.returncode == 0 else f"not installed or not running ({text[:200]})"

    def installed(self) -> bool:
        if self.kind == "linux":
            return self.unit_path.exists()
        if self.kind == "macos":
            return self.plist_path.exists()
        return self.run(["schtasks", "/Query", "/TN", TASK]).returncode == 0


def tail(path: Path, n: int = 60) -> str:
    if not path.exists():
        return f"{path} does not exist yet"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
