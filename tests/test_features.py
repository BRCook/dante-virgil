"""A group switches a feature on only once every member supports it (see virgil.features)."""

import asyncio
import dataclasses
import time

import pytest

import virgil.daemon as daemon_module
from tests.test_daemon import make_pair, start
from virgil import config as config_file
from virgil import features, shared
from virgil.config import NtpKey
from virgil.daemon import Daemon, Peer
from virgil.election import State
from virgil.group import create_invite
from virgil.joining import JoinError, handle_join
from virgil.ntpauth import new_key


def test_versions_and_features():
    assert features.supports("2.0.0", "ntp_auth") and features.supports("2.1", "strong_hashes")
    assert not features.supports("1.1.0", "ntp_auth") and not features.supports(None, "ntp_auth")
    assert not features.supports("unknown", "ntp_auth")  # a development copy supports nothing new
    assert features.waiting_for("ntp_auth", ["a", "b", "c"], {"a": "2.0.0", "b": "1.1.0"}) == ["b", "c"]
    assert features.too_old_for("1.0.0", ["ntp_auth"]) == ["ntp_auth"] and features.too_old_for("2.0.0", ["ntp_auth"]) == []
    assert features.describe({"feature": "ntp_auth", "label": "Authenticated NTP", "on": False,
                              "waiting_for": [{"name": "Box B", "version": "1.1.0"}, {"name": "Box C", "version": None}]}) \
        == "Authenticated NTP: waiting for Box B to be updated (it runs 1.1.0), and Box C to be heard from"
    on = {"feature": "ntp_auth", "label": "Authenticated NTP", "on": True}
    assert features.describe({**on, "waiting_for": [{"name": "Box C", "version": None}]}) == "Authenticated NTP: on"
    assert features.describe({**on, "waiting_for": [{"name": "Box B", "version": "1.1.0"}]}).endswith("update it")


def test_features_and_the_key_are_kept_and_checked(tmp_path):
    config = config_file.new_config()
    config.features, config.ntp_key = ["ntp_auth"], new_key()
    config.ntp_key = new_key(config.ntp_key)  # with a previous key
    config_file.save(config, tmp_path / "virgil.toml")
    again = config_file.load(tmp_path / "virgil.toml")
    assert again.features == ["ntp_auth"] and again.ntp_key == config.ntp_key and again.ntp_key.previous_key
    text = (tmp_path / "virgil.toml").read_text().replace(config.ntp_key.key, "nothex" * 10 + "abcd")
    (tmp_path / "virgil.toml").write_text(text)
    with pytest.raises(config_file.ConfigError, match="ntp_key"):
        config_file.load(tmp_path / "virgil.toml")


def test_a_new_key_keeps_the_one_it_replaces():
    first = new_key()
    second = new_key(first)
    assert second.previous_id == first.id and second.previous_key == first.key and second.id != first.id
    assert len(second.key) == 64 and second.key != first.key
    assert new_key(NtpKey(65535, "a" * 64)).id == 1  # IDs wrap round within chrony's range


def test_features_merge_as_a_union_and_older_virgils_ignore_them():
    config = config_file.new_config()
    config.features = ["strong_hashes"]
    theirs = shared.parse({"features": {"value": ["ntp_auth", "from_the_future"], "changed": 5.0}})
    assert theirs["features"][0] == ["ntp_auth"]  # one this Virgil doesn't know of can't mean anything here
    assert shared.adopt(config, theirs) == [("features", ["ntp_auth"])]
    assert config.features == ["ntp_auth", "strong_hashes"]
    # Nothing switches a feature off, however recent.
    shared.adopt(config, shared.parse({"features": {"value": [], "changed": 99.0}}))
    assert config.features == ["ntp_auth", "strong_hashes"]
    # A 1.1.0 Virgil reads only the settings it knows, so the new ones don't trouble it.
    snapshot = shared.snapshot(config)
    assert {"auto_trim", "web_password"} <= set(snapshot) and {"features", "ntp_key"} <= set(snapshot)
    with pytest.raises(shared.SharedError):
        shared.parse({"ntp_key": {"value": {"id": 0, "key": "a" * 64}, "changed": 1.0}})


def test_a_joiner_takes_on_the_groups_features_and_key_never_its_own():
    joiner = config_file.new_config()
    joiner.features, joiner.ntp_key = ["ntp_auth"], new_key()  # it switched them on alone, before joining
    group_key = new_key()
    shared.take_on_joining(joiner, {"features": (["ntp_auth"], 7.0), "ntp_key": (group_key, 8.0)}, 10.0)
    assert joiner.features == ["ntp_auth"] and joiner.ntp_key == group_key
    shared.take_on_joining(joiner, {"auto_trim": (True, 1.0)}, 10.0)  # a 1.1.0 group: none of either
    assert joiner.features == [] and joiner.ntp_key is None


def _heard(daemon, other, version):
    """daemon has just heard other's heartbeat, reporting version."""
    beat = dataclasses.replace(other.heartbeat(), version=version)
    daemon.peers[other.config.id] = Peer(beat, time.monotonic())


def test_a_mixed_group_waits_then_switches_on_by_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "__version__", "2.0.0")
    paths, identities, configs = make_pair(tmp_path)
    a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
    a.election.resume(State.LEADER, None, 0.0)

    # Not heard from at all: waiting, and nothing switched on.
    assert a.switch_on_features() == [] and not a.group_supports("ntp_auth")
    _heard(a, b, "1.1.0")  # a 1.1.0 member: still waiting, and the group carries on as before
    assert a.switch_on_features() == [] and a.config.features == [] and a.config.ntp_key is None
    state = {s["feature"]: s for s in a.status_data()["features"]}["ntp_auth"]
    assert features.describe(state) == "Authenticated NTP: waiting for Virgil B to be updated (it runs 1.1.0)"

    _heard(a, b, "2.0.0")  # B is updated: on, by itself, with a key, and recorded
    assert sorted(a.switch_on_features()) == ["ntp_auth", "strong_hashes"]
    assert a.config.ntp_key is not None and a.config.features_changed > 0
    assert any("switched on Authenticated NTP" in e["title"] for e in a.events.recent)
    assert config_file.load(paths["A"]).features == ["ntp_auth", "strong_hashes"]  # saved

    # B learns of it from A's heartbeats, takes on the key, and records it too.
    b.adopt_shared_settings(shared.parse(shared.snapshot(a.config)), a.config.id)
    assert b.config.features == a.config.features and b.config.ntp_key == a.config.ntp_key
    assert any("Authenticated NTP" in e["title"] for e in b.events.recent)


def test_a_member_not_heard_from_lately_holds_features_back(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "__version__", "2.0.0")
    paths, identities, configs = make_pair(tmp_path)
    a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
    a.election.resume(State.LEADER, None, 0.0)
    _heard(a, b, "2.0.0")
    a.peers[b.config.id] = Peer(a.peers[b.config.id].heartbeat, time.monotonic() - features.HEARD_WITHIN - 1)
    assert a.switch_on_features() == []


def test_an_unsettled_virgil_switches_nothing_on(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "__version__", "2.0.0")
    paths, identities, configs = make_pair(tmp_path)
    a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
    _heard(a, b, "2.0.0")  # A is still listening for a leader
    assert a.switch_on_features() == []


def test_a_joiner_too_old_for_the_groups_features_is_refused_and_the_invite_kept():
    config = config_file.new_config()
    config.group = __import__("virgil.group", fromlist=["create"]).create(config.id, config.name, _cert())
    config.features, config.ntp_key = ["ntp_auth"], new_key()
    secret, _ = create_invite(config.group, time.time())
    body = {"secret": secret, "id": "b" * 32, "name": "Box B", "cert": _cert("box-b"), "version": "1.1.0"}
    with pytest.raises(JoinError, match="uses Authenticated NTP.*update it"):
        handle_join(config, body, time.time())
    old = {k: v for k, v in body.items() if k != "version"}  # a 1.x Virgil sends no version at all
    with pytest.raises(JoinError, match="an older version"):
        handle_join(config, old, time.time())
    assert len(config.group.invites) == 1  # refused for its version: the invite is still there for another try
    reply = handle_join(config, {**body, "version": "2.0.0"}, time.time())
    assert "b" * 32 in config.group.members and not config.group.invites
    assert reply["shared_settings"]["features"]["value"] == ["ntp_auth"]


def test_an_invite_survives_a_join_refused_for_a_taken_id():
    """A join refused because another Virgil has the same ID doesn't spend the invite (item 8 of the 2.0 plan)."""
    from virgil.group import create

    config = config_file.new_config()
    config.group = create(config.id, config.name, _cert())
    secret, _ = create_invite(config.group, time.time())
    body = {"secret": secret, "id": config.id, "name": "Impostor", "cert": _cert("other"), "version": "2.0.0"}
    with pytest.raises(JoinError, match="same Virgil ID"):
        handle_join(config, body, time.time())
    assert len(config.group.invites) == 1


_CERTS = {}


def _cert(name="box-a"):
    if name not in _CERTS:
        import tempfile
        from pathlib import Path

        from virgil import tls

        identity = tls.default_identity(Path(tempfile.mkdtemp()) / "tls")
        tls.generate(identity, hostname=name)
        _CERTS[name] = identity.cert_pem
    return _CERTS[name]
