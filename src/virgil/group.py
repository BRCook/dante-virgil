"""The group: its members, invites, and invite tokens.

A member is a box ID, a name and a certificate. There's no shared group key:
members recognise each other by their pinned certificates.

An invite is a random secret that works once and expires. The token handed to
the new box holds the secret, plus what it needs to find the inviting box and
check it's talking to the right one: its addresses, and its certificate's
fingerprint. The inviting box keeps only a hash of the secret.
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


@dataclass
class Group:
    id: str
    members: dict[str, Member] = field(default_factory=dict)
    # Hash of each outstanding invite's secret, and when it expires (Unix time).
    invites: dict[str, int] = field(default_factory=dict)


def create(box_id: str, name: str, cert: str) -> Group:
    return Group(id=secrets.token_hex(16), members={box_id: Member(name, cert)})


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


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


def redeem_invite(group: Group, secret: str, now: float) -> bool:
    """Use up an invite. True if the secret was valid, unused and unexpired."""
    prune_invites(group, now)
    given = _hash(secret)
    for key in group.invites:
        if hmac.compare_digest(key, given):
            del group.invites[key]
            return True
    return False


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
