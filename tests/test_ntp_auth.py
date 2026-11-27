"""Authenticated NTP between members (item 2 of the 2.0 plan)."""

import json
import stat
import time
from dataclasses import replace

import pytest

import virgil.daemon as daemon_module
from tests.test_daemon import make_pair
from tests.test_rootctl import _follower, _leader, _prepare
from virgil.daemon import Daemon
from virgil.election import State
from virgil.generate import Role
from virgil.ntpauth import new_key
from virgil.request import RequestError, from_json, to_json
from virgil.roles import CHRONY, PHC2SYS, REKEY, transition

KEY, OTHER = "a" * 64, "b" * 64


def _check(**fields):
    return from_json(to_json(_follower(**fields)))


def test_the_requests_keys_are_checked_strictly():
    good = _check(ntp_key_id=7, ntp_key=KEY, ntp_previous_key_id=6, ntp_previous_key=OTHER)
    assert (good.ntp_key_id, good.ntp_previous_key_id) == (7, 6)
    for bad, message in (({"ntp_key_id": 7, "ntp_key": KEY.upper()}, "lowercase hex"),
                         ({"ntp_key_id": 7, "ntp_key": KEY[:-1]}, "64 lowercase"),
                         ({"ntp_key_id": 7, "ntp_key": KEY[:-1] + "\n"}, "64 lowercase"),  # no directives smuggled in
                         ({"ntp_key_id": 0, "ntp_key": KEY}, "1 to 65535"),
                         ({"ntp_key": KEY}, "1 to 65535"),
                         ({"ntp_previous_key_id": 6, "ntp_previous_key": OTHER}, "needs a current one"),
                         ({"ntp_key_id": 7, "ntp_key": KEY, "ntp_previous_key_id": 7, "ntp_previous_key": OTHER},
                          "an ID of its own")):
        data = json.loads(to_json(_follower()))
        data.update(bad)
        with pytest.raises(RequestError, match=message):
            from_json(json.dumps(data))


def test_a_followers_chrony_requires_the_key_and_a_leaders_answers_with_it(tmp_path):
    (tmp_path / "f").mkdir()
    (tmp_path / "l").mkdir()
    follower = _prepare(tmp_path / "f", _follower(ntp_key_id=7, ntp_key=KEY, ntp_previous_key_id=6, ntp_previous_key=OTHER))
    conf = (follower / "chrony.conf").read_text()
    assert f"keyfile {follower / 'chrony.keys'}" in conf and " key 7" in conf
    assert "authselectmode require" in conf and "authselectmode ignore" not in conf
    keys = follower / "chrony.keys"
    assert keys.read_text() == f"7 SHA256 HEX:{KEY}\n6 SHA256 HEX:{OTHER}\n"
    assert not stat.S_IMODE(keys.stat().st_mode) & 0o007  # nobody but root (and chrony) can read it
    # Setting the clock reads only its own directives, so the key goes there too.
    sync = json.loads((follower / "plan.json").read_text())["sync"][0]["args"]
    assert sync[-2:] == [f"keyfile {follower / 'chrony.keys'}", "server 192.168.20.21 iburst key 7"]

    leader = _prepare(tmp_path / "l", _leader(ntp_key_id=7, ntp_key=KEY))
    assert f"keyfile {leader / 'chrony.keys'}" in (leader / "chrony.conf").read_text()


def test_without_a_key_nothing_changes_and_an_old_key_file_goes(tmp_path):
    generated = _prepare(tmp_path, _follower(ntp_key_id=7, ntp_key=KEY))
    assert (generated / "chrony.keys").exists()
    request_path = tmp_path / "request.json"
    request_path.write_text(to_json(_follower()))
    from tests.test_rootctl import lookup
    from virgil.rootctl import prepare

    prepare(request_path, generated, lookup)
    conf = (generated / "chrony.conf").read_text()
    assert not (generated / "chrony.keys").exists() and "keyfile" not in conf and "authselectmode ignore" in conf
    sync = json.loads((generated / "plan.json").read_text())["sync"][0]["args"]
    assert sync[-1] == "server 192.168.20.21 iburst"


def test_a_leaders_new_key_is_a_rekey_and_a_followers_a_chrony_restart():
    old_leader = _leader(ntp_key_id=7, ntp_key=KEY)
    new_leader = replace(old_leader, ntp_key_id=8, ntp_key=OTHER, ntp_previous_key_id=7, ntp_previous_key=KEY)
    steps = transition(old_leader, new_leader)
    assert [s.unit for s in steps if s.action != "write"][-1] == REKEY
    assert not any(s.action in ("restart", "stop") for s in steps)  # the leader's chrony keeps serving

    old_follower = _follower(ntp_key_id=7, ntp_key=KEY)
    new_follower = replace(old_follower, ntp_key_id=8, ntp_key=OTHER, ntp_previous_key_id=7, ntp_previous_key=KEY)
    steps = transition(old_follower, new_follower)
    assert any(s.action == "restart" and s.unit == CHRONY for s in steps)
    assert not any(s.unit == PHC2SYS and s.action in ("restart", "stop") for s in steps)

    # Switching authentication on needs chrony to read a key file at all: one restart.
    steps = transition(_leader(), old_leader)
    assert any(s.action == "restart" and s.unit == CHRONY for s in steps) and REKEY not in [s.unit for s in steps]


def test_virgild_sends_keys_only_once_the_group_authenticates(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "__version__", "2.0.0")
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.config.ptp_interface = "enp1s0"
    a.config.ntp_key = new_key()
    assert a.ntp_keys() == {}  # a key, but the feature isn't on
    a.config.features = ["ntp_auth"]
    assert a.ntp_keys()["ntp_key"] == a.config.ntp_key.key


def test_removing_a_member_changes_the_key_and_leaving_drops_it(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(daemon_module, "__version__", "2.0.0")
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.election.resume(State.LEADER, None, 0.0)
    a.config.features, a.config.ntp_key = ["ntp_auth"], new_key()
    first = a.config.ntp_key

    async def remove():
        async def nobody(*args, **kwargs):
            return []
        a.push_members = nobody
        await a.remove_member(configs["B"].id)

    asyncio.run(remove())
    assert a.config.ntp_key.previous_key == first.key and a.config.ntp_key.key != first.key
    assert any("changed the group's NTP key" in e["title"] for e in a.events.recent)

    a._leave_locally()
    assert a.config.ntp_key is None and a.config.features == []
