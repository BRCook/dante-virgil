"""Settings every member of a group shares, kept in step the same way as the member list.

Most settings describe one Clock Box (its interfaces, its NICs, its own Dante
devices), so they stay with it. These don't:

- `auto_trim` only matters on the leader, and leadership moves. If it were on in
  one Clock Box and off in another, auto-trim would quietly switch on and off
  whenever the lead changed hands.
- The web UI's password: members already trust each other with far more (setting
  the time, changing each other's devices), and setting it once for the group is
  what anyone would expect. It travels as its scrypt hash, and only between
  members, over their mutual TLS.
- The features switched on for the group (see virgil.features). Unlike the others,
  these merge as a union: features are only ever added, so nothing can switch one
  back off, and two members switching on different ones at once both count.
- The group's NTP key, for authenticating NTP between members (see the design
  document's "Authenticated NTP"). Like the password, it only travels between
  members, over their mutual TLS.

An older Virgil ignores settings it doesn't know, so a group with members on
different versions keeps working.

Each records when it was last changed. Heartbeats carry them, and a member that
hears a newer change adopts it, so a change made anywhere reaches everyone,
including members that were offline at the time.

"When" is by the clock of the Virgil that made the change, so three rules keep a
wrong clock from winning every comparison (a newcomer whose clock is years ahead,
say, whose every change would otherwise look the newest):

- A Virgil joining a group takes on the group's settings outright (take_on_joining).
- Only a Virgil whose clock has settled with its group can change them
  (clock_settled). That's for changes people make, from any member; changes the
  group makes by itself are the leader's (see virgil.daemon's acts_for_group).
- A change is always stamped later than the one it replaces (next_stamp), so a
  group whose time was once ahead, and then corrected, can still be changed.
"""

from __future__ import annotations

import re
from typing import Any

from virgil.config import Config, NtpKey, valid_ntp_key
from virgil.features import FEATURES

# As virgil.webauth makes them: scrypt$N$r$p$salt$digest, in base64.
SCRYPT_HASH = re.compile(r"^scrypt\$\d{1,7}\$\d{1,3}\$\d{1,3}\$[A-Za-z0-9+/_=-]{1,64}\$[A-Za-z0-9+/_=-]{1,128}$")


class SharedError(ValueError):
    pass


# Why a Virgil whose clock hasn't settled refuses to change what its group shares (see clock_settled).
UNSETTLED = ("this Virgil's clock hasn't settled with its group yet, so it can't change what the group shares: "
             "wait a minute or so, or do it on another member")


# The shared settings, in the order they're described.
NAMES = ("auto_trim", "web_password", "features", "ntp_key")


def _key_value(key: NtpKey | None) -> dict[str, Any] | None:
    if key is None:
        return None
    return {"id": key.id, "key": key.key, "previous_id": key.previous_id, "previous_key": key.previous_key}


def snapshot(config: Config) -> dict[str, dict[str, Any]]:
    return {
        "auto_trim": {"value": config.auto_trim, "changed": config.auto_trim_changed},
        "web_password": {"value": config.web_password, "changed": config.web_password_changed},
        "features": {"value": sorted(config.features), "changed": config.features_changed},
        "ntp_key": {"value": _key_value(config.ntp_key), "changed": config.ntp_key_changed},
    }


def _parse_key(value: Any) -> NtpKey | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SharedError("a bad shared setting: ntp_key")
    key = NtpKey(value.get("id"), value.get("key"), value.get("previous_id"), value.get("previous_key"))
    if not valid_ntp_key(key):
        raise SharedError("a bad shared setting: ntp_key")
    return key


def _parse_features(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 64 or not all(isinstance(f, str) and len(f) <= 64 for f in value):
        raise SharedError("a bad shared setting: features")
    return sorted({f for f in value if f in FEATURES})  # one this Virgil doesn't know of can't mean anything here


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
    for name, convert in (("features", _parse_features), ("ntp_key", _parse_key)):
        entry = data.get(name)
        if entry is None:
            continue
        changed = entry.get("changed") if isinstance(entry, dict) else None
        if isinstance(changed, bool) or not isinstance(changed, (int, float)):
            raise SharedError(f"a bad shared setting: {name}")
        parsed[name] = (convert(entry.get("value")), float(changed))
    return parsed


def adopt(config: Config, theirs: dict[str, tuple[Any, float]]) -> list[tuple[str, Any]]:
    """Take on any setting another member changed more recently. Returns what changed, as (name, new value)."""
    adopted = []
    for name, (value, changed) in theirs.items():
        if name == "features":
            # A union: features are only ever added, so nothing can switch one back off.
            added = sorted(set(value) - set(config.features))
            if added:
                config.features = sorted(set(config.features) | set(value))
                adopted.append((name, added))
            config.features_changed = max(config.features_changed, changed)
            continue
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
    for name in NAMES:
        stamp = f"{name}_changed"
        if name == "features":
            # Never this Virgil's own: features are the group's, switched on by its members (see virgil.features).
            config.features = list(theirs[name][0]) if theirs is not None and name in theirs else []
            config.features_changed = theirs[name][1] if theirs is not None and name in theirs else 0.0
            continue
        if name == "ntp_key":
            # Likewise the group's: one this Virgil brings would only be a key the rest don't share.
            config.ntp_key = theirs[name][0] if theirs is not None and name in theirs else None
            config.ntp_key_changed = theirs[name][1] if theirs is not None and name in theirs else 0.0
            continue
        if theirs is not None and name in theirs and theirs[name][1] > 0:
            setattr(config, name, theirs[name][0])
            setattr(config, stamp, theirs[name][1])
        elif their_now is not None:
            setattr(config, stamp, min(getattr(config, stamp), their_now))
        else:
            setattr(config, stamp, 0.0)


def describe(name: str, value: Any) -> str:
    """A change, in words, for the event history."""
    if name == "features":
        from virgil.features import LABELS

        return "switched on " + " and ".join(LABELS.get(f, f) for f in value) + " for the group"
    if name == "ntp_key":
        return "changed the group's NTP key"
    if name == "auto_trim":
        return f"turned automatic trim {'on' if value else 'off'} for the group"
    return "changed the web UI's password for the group" if value else "removed the web UI's password for the group"
