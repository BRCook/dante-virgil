"""The web UI's password, sessions, and limits on guessing.

Anyone can read a Virgil's status; changing anything needs the password.
The password is stored as a scrypt hash in the config (web_password). Logging
in gives a session cookie that's only sent over HTTPS, can't be read by
scripts, and is only sent by the Virgil's own pages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable

MIN_PASSWORD = 8
SESSION_LIFETIME = 12 * 3600
COOKIE = "virgil_session"
# At most this many wrong passwords per address, per window.
ATTEMPTS = 5
ATTEMPT_WINDOW = 60.0

_N, _R, _P, _LENGTH = 2**14, 8, 1, 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_LENGTH)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class Sessions:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        self._tidy()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = self._clock() + SESSION_LIFETIME
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._sessions.get(token)
        if expires is None:
            return False
        if expires <= self._clock():
            del self._sessions[token]
            return False
        return True

    def end(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def end_all(self) -> None:
        """Such as when the password changes."""
        self._sessions.clear()

    def _tidy(self) -> None:
        now = self._clock()
        for token in [t for t, expires in self._sessions.items() if expires <= now]:
            del self._sessions[token]


class Attempts:
    """Wrong passwords per address, to slow down guessing."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, address: str) -> deque[float]:
        failures = self._failures.setdefault(address, deque())
        cutoff = self._clock() - ATTEMPT_WINDOW
        while failures and failures[0] < cutoff:
            failures.popleft()
        return failures

    def blocked(self, address: str) -> bool:
        return len(self._recent(address)) >= ATTEMPTS

    def failed(self, address: str) -> None:
        self._recent(address).append(self._clock())

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)
