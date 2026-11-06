"""Joining a group with an invite token.

The new box connects to the inviting box's public port, checks its
certificate against the fingerprint in the token, and sends the invite's
secret along with its own certificate. The inviting box checks the secret,
adds the new box as a member, and replies with the member list.
"""

from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

from virgil.config import Config
from virgil.group import Group, GroupError, InviteToken, Member, redeem_invite
from virgil.tls import TlsError, fingerprint

BOX_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_NAME = 64
JOIN_PATH = "/api/v1/join"


class JoinError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _members_json(config: Config) -> dict[str, dict[str, str]]:
    return {box_id: {"name": m.name, "cert": m.cert} for box_id, m in config.group.members.items()}


def join_request(config: Config, cert: str, secret: str) -> dict[str, str]:
    return {"secret": secret, "id": config.id, "name": config.name, "cert": cert}


def handle_join(config: Config, body: Any, now: float) -> dict[str, Any]:
    """On the inviting box: check a join request, and add the new member to config.group."""
    if config.group is None:
        raise JoinError(409, "this box isn't in a group")
    if not isinstance(body, dict):
        raise JoinError(400, "the request must be an object")
    secret, box_id, name, cert = (body.get(k) for k in ("secret", "id", "name", "cert"))
    if not all(isinstance(v, str) for v in (secret, box_id, name, cert)):
        raise JoinError(400, "the request needs a secret, id, name and cert")
    if not BOX_ID.match(box_id):
        raise JoinError(400, "bad box ID")
    if not 1 <= len(name) <= MAX_NAME:
        raise JoinError(400, "bad name")
    try:
        fingerprint(cert)
    except TlsError as error:
        raise JoinError(400, "bad certificate") from error

    # Check the secret before revealing anything else about the group.
    if not redeem_invite(config.group, secret, now):
        raise JoinError(403, "that invite isn't valid, has been used, or has expired")

    existing = config.group.members.get(box_id)
    if existing is not None and fingerprint(existing.cert) != fingerprint(cert):
        raise JoinError(409, "a different box with the same box ID is already a member")
    config.group.members[box_id] = Member(name, cert)
    return {"group_id": config.group.id, "members": _members_json(config)}


def apply_join_response(config: Config, token: InviteToken, response: Any) -> None:
    """On the new box: check the inviting box's reply, and take on the group."""
    if not isinstance(response, dict) or response.get("group_id") != token.group_id:
        raise GroupError("the inviting box replied about a different group")
    members_data = response.get("members")
    if not isinstance(members_data, dict) or config.id not in members_data:
        raise GroupError("the inviting box didn't add this box to the group")
    members = {}
    for box_id, member in members_data.items():
        if not (isinstance(box_id, str) and BOX_ID.match(box_id) and isinstance(member, dict)):
            raise GroupError("the inviting box sent a bad member list")
        name, cert = member.get("name"), member.get("cert")
        if not (isinstance(name, str) and isinstance(cert, str)):
            raise GroupError("the inviting box sent a bad member list")
        try:
            fingerprint(cert)
        except TlsError as error:
            raise GroupError(f"the inviting box sent a bad certificate for {name}") from error
        members[box_id] = Member(name, cert)
    if not any(fingerprint(m.cert) == token.fingerprint for m in members.values()):
        raise GroupError("the inviting box isn't in its own member list")
    config.group = Group(id=token.group_id, members=members)


async def join(token: InviteToken, config: Config, cert: str, timeout: float = 10.0) -> None:
    """Join the group in the token, updating config. Tries each of the inviting box's addresses."""
    if config.group is not None:
        raise GroupError("this box is already in a group")
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
                        raise GroupError(f"the inviting box refused: {message or reply.status}")
                    apply_join_response(config, token, data)
                    return
            except aiohttp.ServerFingerprintMismatch as error:
                raise GroupError(
                    f"the box at {address} isn't the one that made the invite (its certificate doesn't match)"
                ) from error
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                errors.append(f"{address}: {error or type(error).__name__}")
    raise GroupError("couldn't reach the inviting box (" + "; ".join(errors) + ")")
