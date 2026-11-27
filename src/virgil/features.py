"""What a group can switch on, when its members may run different versions.

Clock Boxes are updated one at a time, so for a while a group can hold members on
different versions. Some features break a group in that state: a follower that
requires an NTP key can't sync to a leader without one, and a member on an older
version can't check a password hashed with stronger settings. So such a feature is
switched on for the group only once every member supports it:

- Each feature names the version that introduced it (FEATURES).
- A member supports it when it reports that version or later in its heartbeats. A
  member not heard from lately supports nothing: a feature is never switched on
  because someone is merely offline.
- Once every member supports it, the first member whose clock has settled switches
  it on, with nobody needing to do anything. It's recorded in the shared settings
  (see virgil.shared), so it stays on: shared features are only ever added to.
- A Virgil asking to join is refused if it's too old for a feature the group uses,
  with a message saying to update it first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from virgil.updates import parse_version

# Each feature, and the version that introduced it.
FEATURES: dict[str, tuple[int, ...]] = {
    "ntp_auth": (2, 0),
    "strong_hashes": (2, 0),
}

# Each feature's name, in words.
LABELS = {
    "ntp_auth": "Authenticated NTP",
    "strong_hashes": "Stronger password hashes",
}

# How recently a member must have been heard from to count as supporting anything.
HEARD_WITHIN = 60.0


def supports(version: str | None, feature: str) -> bool:
    """Whether a Virgil reporting this version supports a feature. A version that isn't a plain release number
    (a development copy's "unknown", say) supports nothing new."""
    found = parse_version(version)
    needed = FEATURES[feature]
    if found is None:
        return False
    width = max(len(found), len(needed))
    return found + (0,) * (width - len(found)) >= needed + (0,) * (width - len(needed))


def waiting_for(feature: str, members: Iterable[str], versions: Mapping[str, str | None]) -> list[str]:
    """The members a feature is waiting for: those not heard from lately (missing from versions, or None), and
    those whose version is too old. versions maps a member's Virgil ID to what it reports, this Virgil included."""
    return [m for m in members if not supports(versions.get(m), feature)]


def too_old_for(version: str | None, features_on: Iterable[str]) -> list[str]:
    """Which of the group's features a Virgil of this version doesn't support (to refuse a join)."""
    return [f for f in features_on if f in FEATURES and not supports(version, f)]


def describe(state: dict) -> str:
    """A feature's state, from virgild's status, in words."""
    label = state.get("label") or LABELS.get(state.get("feature"), "?")
    waiting = [(w.get("name") or "?", w.get("version")) for w in state.get("waiting_for") or []]
    if not state.get("on"):
        return describe_waiting(state.get("feature"), waiting) if waiting else f"{label}: about to switch on"
    too_old = [f"{name} runs {version}" for name, version in waiting if version]
    if too_old:
        return f"{label}: on, but " + ", and ".join(too_old) + ", which doesn't support it: update it"
    return f"{label}: on"


def describe_waiting(feature: str, waiting: list[tuple[str, str | None]]) -> str:
    """Why a feature isn't on yet, in words: [(member's name, its version, or None if not heard from)]."""
    parts = [f"{name} to be updated (it runs {version})" if version else f"{name} to be heard from"
             for name, version in waiting]
    return f"{LABELS.get(feature, feature)}: waiting for " + ", and ".join(parts)
