import base64
import json

import pytest

from virgil import group as groups
from virgil.group import GroupError, InviteToken

CERT = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"


def test_create():
    group = groups.create("box-a", "Box A", CERT)
    assert len(group.id) == 32
    assert group.members["box-a"].name == "Box A"


def test_an_invite_works_once():
    group = groups.create("a", "A", CERT)
    secret, expires = groups.create_invite(group, now=1000)
    assert expires == 1000 + groups.INVITE_LIFETIME
    assert secret not in json.dumps(group.invites)  # only the hash is kept
    assert groups.redeem_invite(group, secret, now=1001)
    assert not groups.redeem_invite(group, secret, now=1002)


def test_an_invite_expires():
    group = groups.create("a", "A", CERT)
    secret, _ = groups.create_invite(group, now=1000, lifetime=60)
    assert not groups.redeem_invite(group, secret, now=1060)
    assert group.invites == {}


def test_a_wrong_secret_fails():
    group = groups.create("a", "A", CERT)
    groups.create_invite(group, now=1000)
    assert not groups.redeem_invite(group, "guess", now=1001)
    assert len(group.invites) == 1


TOKEN = InviteToken("0123456789abcdef0123456789abcdef", ("192.168.20.21",), 8740, "ab" * 32, "s" * 43, 2000)


def test_token_round_trip():
    encoded = TOKEN.encode()
    assert encoded.startswith("virgil1.")
    assert InviteToken.decode(encoded) == TOKEN
    assert InviteToken.decode(f"  {encoded}\n") == TOKEN


def _token(**changes):
    data = {"g": TOKEN.group_id, "a": list(TOKEN.addresses), "p": 8740, "f": TOKEN.fingerprint,
            "s": TOKEN.secret, "x": 2000, **changes}
    data = {k: v for k, v in data.items() if v is not ...}
    return "virgil1." + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        "hello",
        "virgil1.!!!",
        "virgil1." + base64.urlsafe_b64encode(b"[1,2]").decode(),
        _token(g="nope"),
        _token(a=[]),
        _token(a=["box-a.local"]),
        _token(a="192.168.20.21"),
        _token(p=0),
        _token(p=True),
        _token(f="xyz"),
        _token(s="short"),
        _token(x="tomorrow"),
        _token(s=...),
    ],
)
def test_bad_tokens(token):
    with pytest.raises(GroupError):
        InviteToken.decode(token)
