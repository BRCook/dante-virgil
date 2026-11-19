"""Settings every member of a group shares, kept in step the same way as the member list.

Most settings describe one Clock Box (its interfaces, its network cards, its own
Dante devices), so they stay with it. Two don't:

- `auto_trim` only matters on the leader, and leadership moves. If it were on in
  one Clock Box and off in another, auto-trim would quietly switch on and off
  whenever the lead changed hands.
- The web UI's password: members already trust each other with far more (setting
  the time, changing each other's devices), and setting it once for the group is
  what anyone would expect. It travels as its scrypt hash, and only between
  members, over their mutual TLS.

Each records when it was last changed. Heartbeats carry them, and a member that
hears a newer change adopts it, so a change made anywhere reaches everyone,
including members that were offline at the time.
"""

from __future__ import annotations

import re
from typing import Any

from virgil.config import Config

# As virgil.webauth makes them: scrypt$N$r$p$salt$digest, in base64.
SCRYPT_HASH = re.compile(r"^scrypt\$\d{1,7}\$\d{1,3}\$\d{1,3}\$[A-Za-z0-9+/_=-]{1,64}\$[A-Za-z0-9+/_=-]{1,128}$")


class SharedError(ValueError):
    pass


def snapshot(config: Config) -> dict[str, dict[str, Any]]:
    return {
        "auto_trim": {"value": config.auto_trim, "changed": config.auto_trim_changed},
        "web_password": {"value": config.web_password, "changed": config.web_password_changed},
    }


def parse(data: Any) -> dict[str, tuple[Any, float]]:
    """Check another member's shared settings: {name: (value, changed)}."""
    if not isinstance(data, dict):
        raise SharedError("shared settings must be an object")
    parsed = {}
    for name, check in (("auto_trim", lambda v: isinstance(v, bool)),
                        ("web_password", lambda v: v is None or (isinstance(v, str) and SCRYPT_HASH.match(v)))):
        entry = data.get(name)
        if entry is None:
            continue
        changed = entry.get("changed") if isinstance(entry, dict) else None
        if isinstance(changed, bool) or not isinstance(changed, (int, float)) or not check(entry.get("value")):
            raise SharedError(f"a bad shared setting: {name}")
        parsed[name] = (entry.get("value"), float(changed))
    return parsed


def adopt(config: Config, theirs: dict[str, tuple[Any, float]]) -> list[tuple[str, Any]]:
    """Take on any setting another member changed more recently. Returns what changed, as (name, new value)."""
    adopted = []
    for name, (value, changed) in theirs.items():
        if changed > getattr(config, f"{name}_changed"):
            setattr(config, f"{name}_changed", changed)
            if getattr(config, name) != value:
                setattr(config, name, value)
                adopted.append((name, value))
    return adopted


def describe(name: str, value: Any) -> str:
    """A change, in words, for the event history."""
    if name == "auto_trim":
        return f"turned automatic trim {'on' if value else 'off'} for the group"
    return "changed the web UI's password for the group" if value else "removed the web UI's password for the group"
