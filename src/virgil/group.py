"""The group: its members, invites, and invite tokens.

A member is a Virgil ID, a name and a certificate. There's no shared group key:
members recognise each other by their pinned certificates.

An invite is a random secret that works once and expires. The token handed to
the new Virgil holds the secret, plus what it needs to find the inviting Virgil and
check it's talking to the right one: its addresses, and its certificate's
fingerprint. The inviting Virgil keeps only a hash of the secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass, field

PUBLIC_PORT = 8740
GROUP_PORT = 8741
INVITE_LIFETIME = 3600
TOKEN_PREFIX = "virgil1."

HEX_ID = re.compile(r"^[0-9a-f]{32}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class GroupError(Exception):
    pass


@dataclass
class Member:
    name: str
    cert: str
    # When this entry last changed (Unix time): the latest change wins when members' lists are merged.
    changed: float = 0.0


@dataclass
class Removed:
    changed: float
    fingerprint: str | None = None  # the removed Virgil's certificate's, so it can be recognised
    by: str | None = None  # the Virgil that removed it: itself, if it left


@dataclass
class Group:
    id: str
    members: dict[str, Member] = field(default_factory=dict)
    # Hash of each outstanding invite's secret, and when it expires (Unix time).
    invites: dict[str, int] = field(default_factory=dict)
    # Members that have been removed, and when (see virgil.membership).
    removed: dict[str, Removed] = field(default_factory=dict)


def create(virgil_id: str, name: str, cert: str, now: float = 0.0) -> Group:
    return Group(id=secrets.token_hex(16), members={virgil_id: Member(name, cert, now)})


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def invite_id(key_or_secret: str, is_secret: bool = False) -> str:
    """A short name for an outstanding invite, from its hash: enough to tell invites apart, nothing about the secret."""
    return (_hash(key_or_secret) if is_secret else key_or_secret)[:12]


def outstanding_invites(group: Group, now: float) -> list[dict[str, int | str]]:
    return [{"id": invite_id(key), "expires": expires} for key, expires in sorted(group.invites.items()) if expires > now]


def prune_invites(group: Group, now: float) -> None:
    for key, expires in list(group.invites.items()):
        if expires <= now:
            del group.invites[key]


def create_invite(group: Group, now: float, lifetime: int = INVITE_LIFETIME) -> tuple[str, int]:
    """Add an invite to the group, and return its secret and expiry time."""
    prune_invites(group, now)
    secret = secrets.token_urlsafe(32)
    expires = int(now) + lifetime
    group.invites[_hash(secret)] = expires
    return secret, expires


def valid_invite(group: Group, secret: str, now: float) -> str | None:
    """Whether a secret matches a valid, unused and unexpired invite, without using it up: the invite's key, or
    None. Joining checks this first, then everything else, and only then uses the invite (redeem_invite), so a join
    refused for another reason leaves the invite for another try."""
    prune_invites(group, now)
    given = _hash(secret)
    found = None
    for key in group.invites:
        if hmac.compare_digest(key, given):
            found = key
    return found


def redeem_invite(group: Group, secret: str, now: float) -> bool:
    """Use up an invite. True if the secret was valid, unused and unexpired."""
    key = valid_invite(group, secret, now)
    if key is None:
        return False
    del group.invites[key]
    return True


@dataclass(frozen=True)
class InviteToken:
    group_id: str
    addresses: tuple[str, ...]
    port: int
    fingerprint: str
    secret: str
    expires: int

    def encode(self) -> str:
        data = {
            "g": self.group_id,
            "a": list(self.addresses),
            "p": self.port,
            "f": self.fingerprint,
            "s": self.secret,
            "x": self.expires,
        }
        raw = json.dumps(data, separators=(",", ":")).encode()
        return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> InviteToken:
        token = token.strip()
        if not token.startswith(TOKEN_PREFIX):
            raise GroupError("that isn't a Virgil invite token")
        body = token[len(TOKEN_PREFIX):]
        try:
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (binascii.Error, ValueError) as error:
            raise GroupError("the invite token is damaged") from error
        if not isinstance(data, dict):
            raise GroupError("the invite token is damaged")
        try:
            group_id, addresses, port = data["g"], data["a"], data["p"]
            fingerprint, secret, expires = data["f"], data["s"], data["x"]
        except KeyError as error:
            raise GroupError("the invite token is incomplete") from error
        if not isinstance(group_id, str) or not HEX_ID.match(group_id):
            raise GroupError("the invite token has a bad group ID")
        if not isinstance(fingerprint, str) or not FINGERPRINT.match(fingerprint):
            raise GroupError("the invite token has a bad fingerprint")
        if not isinstance(secret, str) or not 20 <= len(secret) <= 100:
            raise GroupError("the invite token has a bad secret")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise GroupError("the invite token has a bad port")
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise GroupError("the invite token has a bad expiry time")
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
            raise GroupError("the invite token has no addresses")
        try:
            parsed = tuple(str(ipaddress.ip_address(a)) for a in addresses)
        except (TypeError, ValueError) as error:
            raise GroupError("the invite token has a bad address") from error
        return cls(group_id, parsed, port, fingerprint, secret, expires)
