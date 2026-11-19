"""PIN and session cookie for the control centre.

The PIN is never stored: .env holds CONTROL_PIN_HASH = pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>,
written by `python -m platform_core set-pin`. A successful login sets an HttpOnly, SameSite=Strict cookie
signed with CONTROL_SECRET (HMAC-SHA256) that expires after 12 hours. Five wrong PINs lock logins for
15 minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Mapping, Optional

ITERATIONS = 240_000
COOKIE = "cc_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


def hash_pin(pin: str, salt: Optional[bytes] = None, iterations: int = ITERATIONS) -> str:
    if not pin or not pin.isdigit() or not 4 <= len(pin) <= 12:
        raise ValueError("the PIN must be 4 to 12 digits")
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        algo, it, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", (pin or "").encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), want)
    except (ValueError, AttributeError):
        return False


def sign(secret: str, now: Optional[float] = None) -> str:
    exp = int((now or time.time()) + SESSION_SECONDS)
    nonce = secrets.token_hex(8)
    msg = f"{exp}.{nonce}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def check(secret: str, token: Optional[str], now: Optional[float] = None) -> bool:
    if not token or not secret:
        return False
    try:
        exp, nonce, sig = token.split(".")
        want = hmac.new(secret.encode(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want) and int(exp) > (now or time.time())
    except ValueError:
        return False


class Lockout:
    """Wrong-PIN counter kept in the platform state table, so a restart does not reset it."""

    def __init__(self, pengine, key: str = "control_login_failures"):
        self.pengine, self.key = pengine, key

    def _get(self) -> dict:
        from ..db import get_state

        return get_state(self.pengine, self.key, {"n": 0, "until": 0}) or {"n": 0, "until": 0}

    def locked(self, now: Optional[float] = None) -> float:
        """Seconds until logins open again (0 when open)."""
        return max(0.0, self._get().get("until", 0) - (now or time.time()))

    def fail(self, now: Optional[float] = None) -> None:
        from datetime import datetime

        from ..db import put_state

        now = now or time.time()
        st = self._get()
        n = st.get("n", 0) + 1
        until = now + LOCK_SECONDS if n >= MAX_FAILURES else 0
        put_state(self.pengine, self.key, {"n": 0 if until else n, "until": until}, datetime.now())

    def success(self) -> None:
        from datetime import datetime

        from ..db import put_state

        put_state(self.pengine, self.key, {"n": 0, "until": 0}, datetime.now())


def write_env(env_path: Path, values: Mapping[str, str]) -> None:
    """Set keys in a .env file, keeping every other line as it is."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    done = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
