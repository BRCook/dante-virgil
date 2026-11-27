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

# For new passwords; existing ones of 8 or more still work.
MIN_PASSWORD = 12
SESSION_LIFETIME = 12 * 3600
COOKIE = "virgil_session"
# At most this many wrong passwords per address, per window.
ATTEMPTS = 5
ATTEMPT_WINDOW = 60.0

# Hashes are made with one of two settings (scrypt's N, r and p). LEGACY is what Virgil 1.x made, and the only
# setting a 1.x member can check; a group moves to STRONG once every member runs 2.0 (the "strong_hashes" feature,
# see virgil.features). STRONG is one of OWASP's recommended settings: the one needing least memory (32 MiB) and
# least time, since every recommended setting costs several times LEGACY's, and Clock Boxes can be small.
LEGACY = (2**14, 8, 1)
STRONG = (2**15, 8, 3)
_LENGTH = 32
# How much memory checking a hash may use. Python's default (32 MiB) is just under what the next step up from
# today's settings needs (N=2**15 with r=8), so without this, a group couldn't move to stronger hashes until every
# member had been updated at once: a member still on the default couldn't check a password set on a newer one.
# Hashes are still made with the settings above; this only lets every Virgil from here on check stronger ones.
_MAX_MEMORY = 256 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str, strong: bool = False) -> str:
    """A hash of the password: with STRONG's settings once the group can check them (see LEGACY and STRONG)."""
    n, r, p = STRONG if strong else LEGACY
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAX_MEMORY, dklen=_LENGTH)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def weaker_than_strong(stored: str | None) -> bool:
    """Whether a stored hash was made with less work than STRONG's, so is worth upgrading on the next login."""
    try:
        scheme, n, r, p, _salt, _digest = (stored or "").split("$")
        n, r, p = int(n), int(r), int(p)
    except ValueError:
        return False
    strong_n, strong_r, strong_p = STRONG
    return scheme == "scrypt" and (n * r * p < strong_n * strong_r * strong_p or n * r < strong_n * strong_r)


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=_MAX_MEMORY, dklen=len(expected))
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

    def _prune(self) -> None:
        """Forget addresses with no recent wrong passwords, so the table can't grow with every address that tries."""
        cutoff = self._clock() - ATTEMPT_WINDOW
        for address in [a for a, failures in self._failures.items() if not failures or failures[-1] < cutoff]:
            del self._failures[address]

    def blocked(self, address: str) -> bool:
        self._prune()
        return len(self._failures.get(address) or ()) >= ATTEMPTS and len(self._recent(address)) >= ATTEMPTS

    def failed(self, address: str) -> None:
        self._recent(address).append(self._clock())

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)
