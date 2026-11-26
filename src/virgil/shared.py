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

"When" is by the clock of the Virgil that made the change, so three rules keep a
wrong clock from winning every comparison (a newcomer whose clock is years ahead,
say, whose every change would otherwise look the newest):

- A Virgil joining a group takes on the group's settings outright (take_on_joining).
- Only a Virgil whose clock has settled with its group can change them
  (clock_settled).
- A change is always stamped later than the one it replaces (next_stamp), so a
  group whose time was once ahead, and then corrected, can still be changed.
"""

from __future__ import annotations

import re
from typing import Any

from virgil.config import Config

# As virgil.webauth makes them: scrypt$N$r$p$salt$digest, in base64.
SCRYPT_HASH = re.compile(r"^scrypt\$\d{1,7}\$\d{1,3}\$\d{1,3}\$[A-Za-z0-9+/_=-]{1,64}\$[A-Za-z0-9+/_=-]{1,128}$")


class SharedError(ValueError):
    pass


# Why a Virgil whose clock hasn't settled refuses to change what its group shares (see clock_settled).
UNSETTLED = ("this Virgil's clock hasn't settled with its group yet, so it can't change what the group shares: "
             "wait a minute or so, or do it on another member")


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


def clock_settled(in_group: bool, state: str | None, synchronised: bool) -> bool:
    """Whether a Virgil's clock can stamp changes its group shares (these settings, and the member list): when it
    isn't in a group (it has nothing to share), when it leads (its clock is the group's time), or when it follows
    with chrony synchronised to the leader. Anything else (listening for a leader, setting the clock, stopped) could
    stamp a change years out, which would then win every comparison. virgild and virgilctl both decide by this."""
    return not in_group or state == "leader" or (state == "follower" and synchronised)


def next_stamp(previous: float, now: float) -> float:
    """When a change made now is stamped: now, unless that wouldn't be later than the change it replaces (as after
    the group's time was corrected backwards), in which case just after it."""
    return max(now, previous + 0.001)


def take_on_joining(config: Config, theirs: dict[str, tuple[Any, float]] | None, their_now: float | None) -> None:
    """On a Virgil joining a group: take on the group's settings, whatever this Virgil had, and however recent its
    own changes claim to be. A setting the group has never set keeps this Virgil's value, with its stamp no later
    than the inviting Virgil's clock, so nothing this Virgil brings can claim to be from the future. (Without the
    group's settings, from an older Virgil, this Virgil's stamps are cleared, so the group's win.)"""
    for name in ("auto_trim", "web_password"):
        stamp = f"{name}_changed"
        if theirs is not None and name in theirs and theirs[name][1] > 0:
            setattr(config, name, theirs[name][0])
            setattr(config, stamp, theirs[name][1])
        elif their_now is not None:
            setattr(config, stamp, min(getattr(config, stamp), their_now))
        else:
            setattr(config, stamp, 0.0)


def describe(name: str, value: Any) -> str:
    """A change, in words, for the event history."""
    if name == "auto_trim":
        return f"turned automatic trim {'on' if value else 'off'} for the group"
    return "changed the web UI's password for the group" if value else "removed the web UI's password for the group"
