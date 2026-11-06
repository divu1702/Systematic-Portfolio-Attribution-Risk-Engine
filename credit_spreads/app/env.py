"""Minimal .env loader for API secrets.

Secrets (Kite keys, Telegram token) live in `.env`, which is git-ignored and never committed.
This parser avoids an extra dependency: KEY=VALUE lines, optional quotes, `#` comments.
Values already present in the process environment win over the file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Mapping, Optional

from .config import PROJECT_ROOT

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

# Secrets the system knows about, and which milestone first needs them.
KNOWN_SECRETS: Dict[str, str] = {
    "KITE_API_KEY": "milestone 2 (market data)",
    "KITE_API_SECRET": "milestone 2 (daily login)",
    "TELEGRAM_BOT_TOKEN": "milestone 7 (alerts)",
    "TELEGRAM_CHAT_ID": "milestone 7 (alerts)",
}


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected KEY=VALUE")
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        values[key] = val
    return values


def load_env(path: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return the merged environment: .env file values overlaid by real environment variables."""
    merged = parse_env_file(path or DEFAULT_ENV_PATH)
    merged.update(environ if environ is not None else os.environ)
    return merged
