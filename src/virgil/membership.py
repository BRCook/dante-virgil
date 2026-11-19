"""Keeping every member's list of the group in step.

Each Virgil keeps its own list of the group's members, with their certificates.
Every entry records when it last changed: a member (with its name and
certificate) or a removal (with the removed Virgil's certificate fingerprint).
Two lists are merged by keeping, for each Virgil, whichever entry changed most
recently, so a removal beats the earlier joining, and re-inviting a removed
Virgil beats its removal. Clock Boxes all keep the same time, which is what
makes "most recently" reliable here. If a member and a removal changed at the
same moment, the removal wins.

Changes spread two ways: the member making one tells every member it can reach,
straight away; and every heartbeat carries a short digest of the sender's list,
so a member whose list differs fetches the sender's and merges it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from virgil.group import HEX_ID, Group, GroupError, Member, Removed
from virgil.tls import TlsError, fingerprint

MAX_ENTRIES = 64  # members and removals together: far more than any deployment needs


def snapshot(group: Group) -> dict[str, Any]:
    """The group's list, as members send it to each other."""
    return {
        "group_id": group.id,
        "members": {i: {"name": m.name, "cert": m.cert, "changed": m.changed} for i, m in sorted(group.members.items())},
        "removed": {i: {"changed": r.changed, "fingerprint": r.fingerprint} for i, r in sorted(group.removed.items())},
    }


def digest(group: Group) -> str:
    """A short fingerprint of the list: two members with the same digest have the same list."""
    entries = [(i, "member", m.changed, fingerprint(m.cert), m.name) for i, m in group.members.items()]
    entries += [(i, "removed", r.changed, r.fingerprint, None) for i, r in group.removed.items()]
    return hashlib.sha256(json.dumps(sorted(entries, key=str)).encode()).hexdigest()[:16]


def parse(data: Any, group_id: str) -> tuple[dict[str, Member], dict[str, Removed]]:
    """Check a list another member sent, for this group."""
    if not isinstance(data, dict) or data.get("group_id") != group_id:
        raise GroupError("that list is for a different group")
    members_data, removed_data = data.get("members") or {}, data.get("removed") or {}
    if not isinstance(members_data, dict) or not isinstance(removed_data, dict):
        raise GroupError("a bad member list")
    if len(members_data) + len(removed_data) > MAX_ENTRIES:
        raise GroupError("too many entries in the member list")
    members: dict[str, Member] = {}
    for virgil_id, entry in members_data.items():
        if not (isinstance(virgil_id, str) and HEX_ID.match(virgil_id) and isinstance(entry, dict)):
            raise GroupError("a bad member list")
        name, cert, changed = entry.get("name"), entry.get("cert"), entry.get("changed", 0.0)
        if not (isinstance(name, str) and 1 <= len(name) <= 64 and isinstance(cert, str)
                and isinstance(changed, (int, float)) and not isinstance(changed, bool)):
            raise GroupError("a bad member list")
        try:
            fingerprint(cert)
        except TlsError as error:
            raise GroupError(f"a bad certificate for {name}") from error
        members[virgil_id] = Member(name, cert, float(changed))
    removed: dict[str, Removed] = {}
    for virgil_id, entry in removed_data.items():
        if not (isinstance(virgil_id, str) and HEX_ID.match(virgil_id) and isinstance(entry, dict)):
            raise GroupError("a bad member list")
        changed, fp = entry.get("changed"), entry.get("fingerprint")
        if not isinstance(changed, (int, float)) or isinstance(changed, bool) or not (fp is None or isinstance(fp, str)):
            raise GroupError("a bad member list")
        removed[virgil_id] = Removed(float(changed), fp)
    return members, removed


def merge(group: Group, members: dict[str, Member], removed: dict[str, Removed]) -> list[str]:
    """Merge another member's list into this one, last change winning. Returns what changed, in words."""
    changes = []
    for virgil_id in set(group.members) | set(group.removed) | set(members) | set(removed):
        candidates = []
        for entry in (group.members.get(virgil_id), members.get(virgil_id)):
            if entry is not None:
                candidates.append((entry.changed, 0, entry))  # a removal at the same moment wins (1 > 0)
        for entry in (group.removed.get(virgil_id), removed.get(virgil_id)):
            if entry is not None:
                candidates.append((entry.changed, 1, entry))
        _, _, winner = max(candidates, key=lambda c: (c[0], c[1]))
        if isinstance(winner, Member):
            ours = group.members.get(virgil_id)
            if ours is None or (ours.cert, ours.name, ours.changed) != (winner.cert, winner.name, winner.changed):
                changes.append(f"{winner.name} {'joined' if ours is None else 'changed'}")
                group.members[virgil_id] = Member(winner.name, winner.cert, winner.changed)
            group.removed.pop(virgil_id, None)
        else:
            if virgil_id in group.members:
                changes.append(f"{group.members[virgil_id].name} was removed")
                del group.members[virgil_id]
            ours = group.removed.get(virgil_id)
            if ours is None or ours.changed != winner.changed:
                group.removed[virgil_id] = Removed(winner.changed, winner.fingerprint)
    return changes


def remove(group: Group, virgil_id: str, now: float) -> None:
    """Record a member's removal, as of now."""
    member = group.members.pop(virgil_id, None)
    group.removed[virgil_id] = Removed(now, fingerprint(member.cert) if member else None)
