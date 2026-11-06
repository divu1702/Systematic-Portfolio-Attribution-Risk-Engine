"""Logging: console plus a rotating file under the configured logs directory, IST timestamps."""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Optional

from .clock import IST

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _ISTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        ts = datetime.fromtimestamp(record.created, IST)
        return ts.strftime(datefmt or "%Y-%m-%d %H:%M:%S") + f".{int(record.msecs):03d}"


def setup_logging(log_dir: Optional[Path] = None, level: str = "INFO", console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    fmt = _ISTFormatter(_FORMAT)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "app.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
