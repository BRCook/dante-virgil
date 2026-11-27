"""Joining a group with an invite token.

The new Virgil connects to the inviting Virgil's public port, checks its
certificate against the fingerprint in the token, and sends the invite's
secret along with its own certificate. The inviting Virgil checks the secret,
adds the new Virgil as a member, and replies with the member list.
"""

from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

from virgil import shared
from virgil.config import Config
from virgil.membership import parse, snapshot
from virgil.group import Group, GroupError, InviteToken, Member, redeem_invite
from virgil.tls import TlsError, fingerprint

VIRGIL_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_NAME = 64
JOIN_PATH = "/api/v1/join"


class JoinError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def join_request(config: Config, cert: str, secret: str) -> dict[str, str]:
    return {"secret": secret, "id": config.id, "name": config.name, "cert": cert}


def handle_join(config: Config, body: Any, now: float) -> dict[str, Any]:
    """On the inviting Virgil: check a join request, and add the new member to config.group."""
    if config.group is None:
        raise JoinError(409, "this Virgil isn't in a group")
    if not isinstance(body, dict):
        raise JoinError(400, "the request must be an object")
    secret, virgil_id, name, cert = (body.get(k) for k in ("secret", "id", "name", "cert"))
    if not all(isinstance(v, str) for v in (secret, virgil_id, name, cert)):
        raise JoinError(400, "the request needs a secret, id, name and cert")
    if not VIRGIL_ID.match(virgil_id):
        raise JoinError(400, "bad Virgil ID")
    if not 1 <= len(name) <= MAX_NAME:
        raise JoinError(400, "bad name")
    try:
        fingerprint(cert)
    except TlsError as error:
        raise JoinError(400, "bad certificate") from error

    # Check the secret before revealing anything else about the group.
    if not redeem_invite(config.group, secret, now):
        raise JoinError(403, "that invite isn't valid, has been used, or has expired")

    existing = config.group.members.get(virgil_id)
    if existing is not None and fingerprint(existing.cert) != fingerprint(cert):
        raise JoinError(409, "a different Virgil with the same Virgil ID is already a member")
    # Joining now beats any earlier removal of the same Virgil (see virgil.membership). "Now" alone isn't enough
    # for that: a removal stamped while the group's clock was ahead (before its time was corrected) is later
    # than now, and every other member still holds it, so their lists would have removed the newcomer again
    # the moment they were merged. So, as with every other change the group shares, this one is stamped later
    # than whatever it replaces (see shared.next_stamp).
    earlier = [entry.changed for entry in (existing, config.group.removed.get(virgil_id)) if entry is not None]
    config.group.members[virgil_id] = Member(name, cert, shared.next_stamp(max(earlier, default=0.0), now))
    config.group.removed.pop(virgil_id, None)
    # The group's shared settings, and the time here, which the newcomer takes on (see virgil.shared).
    return {**snapshot(config.group), "shared_settings": shared.snapshot(config), "now": now}


def apply_join_response(config: Config, token: InviteToken, response: Any) -> None:
    """On the new Virgil: check the inviting Virgil's reply, and take on the group."""
    if not isinstance(response, dict) or response.get("group_id") != token.group_id:
        raise GroupError("the inviting Virgil replied about a different group")
    try:
        members, removed = parse(response, token.group_id)
    except GroupError as error:
        raise GroupError(f"the inviting Virgil sent a bad member list: {error}") from error
    if config.id not in members:
        raise GroupError("the inviting Virgil didn't add this Virgil to the group")
    if not any(fingerprint(m.cert) == token.fingerprint for m in members.values()):
        raise GroupError("the inviting Virgil isn't in its own member list")
    theirs, their_now = None, None
    if "shared_settings" in response:
        try:
            theirs = shared.parse(response["shared_settings"])
        except shared.SharedError as error:
            raise GroupError(f"the inviting Virgil sent bad shared settings: {error}") from error
    if isinstance(response.get("now"), (int, float)) and not isinstance(response.get("now"), bool):
        their_now = float(response["now"])
    config.group = Group(id=token.group_id, members=members, removed=removed)
    shared.take_on_joining(config, theirs, their_now)


async def join(token: InviteToken, config: Config, cert: str, timeout: float = 10.0) -> None:
    """Join the group in the token, updating config. Tries each of the inviting Virgil's addresses."""
    if config.group is not None:
        raise GroupError("this Virgil is already in a group")
    if token.expires <= time.time():
        raise GroupError("the invite token has expired")
    pinned = aiohttp.Fingerprint(bytes.fromhex(token.fingerprint))
    body = join_request(config, cert, token.secret)
    errors = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        for address in token.addresses:
            host = f"[{address}]" if ":" in address else address
            url = f"https://{host}:{token.port}{JOIN_PATH}"
            try:
                async with session.post(url, json=body, ssl=pinned) as reply:
                    data = await reply.json(content_type=None)
                    if reply.status != 200:
                        message = data.get("error") if isinstance(data, dict) else None
                        raise GroupError(f"the inviting Virgil refused: {message or reply.status}")
                    apply_join_response(config, token, data)
                    return
            except aiohttp.ServerFingerprintMismatch as error:
                raise GroupError(
                    f"the Virgil at {address} isn't the one that made the invite (its certificate doesn't match)"
                ) from error
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                errors.append(f"{address}: {error or type(error).__name__}")
    raise GroupError("couldn't reach the inviting Virgil (" + "; ".join(errors) + ")")
