"""Alert sink. Milestone 7 adds Telegram; until then alerts go to the log."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Tuple

log = logging.getLogger("alerts")


class Notifier:
    def send(self, text: str, kind: str = "info") -> None:
        raise NotImplementedError


@dataclass
class LogNotifier(Notifier):
    """Logs every alert and keeps them in memory (tests and the simulator read `sent`)."""

    sent: List[Tuple[str, str]] = field(default_factory=list)
    keep: int = 5000

    def send(self, text: str, kind: str = "info") -> None:
        log.info("[%s] %s", kind, text)
        self.sent.append((kind, text))
        if len(self.sent) > self.keep:
            del self.sent[: len(self.sent) - self.keep]
