"""Keeping every member's list of the group in step: the merging, and three real Virgils talking over TLS."""

import asyncio
import copy
import time

import pytest

from virgil import config as config_file
from virgil import membership, tls
from virgil.daemon import Daemon, Peer
from virgil.election import State
from virgil.group import Group, GroupError, Member, Removed
from virgil.joining import handle_join, join_request
from tests.test_daemon import introduce, make_pair, start


def cert(tmp_path, name):
    identity = tls.default_identity(tmp_path / f"cert-{name}")
    tls.generate(identity, hostname=name)
    return identity.cert_pem


A, B, C = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture
def certs(tmp_path):
    return {k: cert(tmp_path, k.lower()) for k in "ABC"}


def test_a_newcomer_spreads(certs):
    ours = Group("g" * 32, {A: Member("A", certs["A"], 1.0), B: Member("B", certs["B"], 1.0)})
    theirs = copy.deepcopy(ours)
    theirs.members[C] = Member("C", certs["C"], 5.0)
    assert membership.digest(ours) != membership.digest(theirs)
    assert membership.merge(ours, theirs.members, theirs.removed) == [("joined", "C joined the group")]
    assert membership.digest(ours) == membership.digest(theirs)
    assert membership.merge(ours, theirs.members, theirs.removed) == []  # nothing new the second time


def test_a_removal_beats_the_joining_before_it(certs):
    ours = Group("g" * 32, {A: Member("A", certs["A"], 1.0), C: Member("C", certs["C"], 5.0)})
    theirs = copy.deepcopy(ours)
    membership.remove(theirs, C, 9.0, by=A)
    assert membership.merge(ours, theirs.members, theirs.removed) == [("removed", "C was removed from the group by A")]
    assert C not in ours.members and ours.removed[C].fingerprint == tls.fingerprint(certs["C"])
    # A stale list that still has C as a member can't bring it back.
    stale = Group("g" * 32, {C: Member("C", certs["C"], 5.0)})
    assert membership.merge(ours, stale.members, stale.removed) == [] and C not in ours.members


def test_reinviting_beats_the_removal(certs):
    ours = Group("g" * 32, {A: Member("A", certs["A"], 1.0)}, removed={C: Removed(9.0)})
    theirs = Group("g" * 32, {C: Member("C", certs["C"], 12.0)})
    assert membership.merge(ours, theirs.members, theirs.removed) == [("joined", "C joined the group")]
    assert C not in ours.removed


def test_a_removal_at_the_same_moment_wins(certs):
    ours = Group("g" * 32, {C: Member("C", certs["C"], 7.0)})
    assert membership.merge(ours, {}, {C: Removed(7.0)}) == [("removed", "C was removed from the group")]


def test_leaving_is_told_apart_from_being_removed(certs):
    ours = Group("g" * 32, {A: Member("A", certs["A"], 1.0), C: Member("C", certs["C"], 1.0)})
    assert membership.merge(ours, {}, {C: Removed(5.0, None, by=C)}) == [("left", "C left the group")]


def test_lists_are_checked(certs):
    group = Group("g" * 32, {A: Member("A", certs["A"], 1.0)})
    good = membership.snapshot(group)
    assert membership.parse(good, "g" * 32)[0][A].cert == certs["A"]
    with pytest.raises(GroupError):
        membership.parse(good, "h" * 32)  # another group's list
    with pytest.raises(GroupError):
        membership.parse({**good, "members": {A: {"name": "A", "cert": "not a certificate"}}}, "g" * 32)
    with pytest.raises(GroupError):
        membership.parse({**good, "members": {f"{i:032x}": {"name": "x", "cert": certs["A"]} for i in range(70)}},
                         "g" * 32)


def test_the_config_keeps_change_times_and_removals(tmp_path, certs):
    config = config_file.new_config()
    config.group = Group("g" * 32, {A: Member("A", certs["A"], 3.5)}, removed={C: Removed(9.0, "f" * 64)})
    config_file.save(config, tmp_path / "virgil.toml")
    loaded = config_file.load(tmp_path / "virgil.toml").group
    assert loaded.members[A].changed == 3.5 and loaded.removed[C] == Removed(9.0, "f" * 64)


# Three real Virgils.


def make_three(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    identity = tls.default_identity(tmp_path / "C" / "tls")
    tls.generate(identity, hostname="c")
    config = config_file.new_config()
    config.name = "Virgil C"
    paths["C"], identities["C"], configs["C"] = tmp_path / "C" / "virgil.toml", identity, config
    config_file.save(config, paths["C"])
    return paths, identities, configs


async def three_running(paths, identities):
    daemons = {name: Daemon(paths[name], identities[name]) for name in "ABC"}
    runners = []
    for daemon in daemons.values():
        runners += await start(daemon)
    return daemons, runners


def c_joins_through_a(daemons, identities):
    """As joining does: A checks C's invite and adds it; C takes A's list."""
    a, c = daemons["A"], daemons["C"]
    from virgil.group import create_invite

    secret, _ = create_invite(a.config.group, time.time())
    reply = handle_join(a.config, join_request(c.config, identities["C"].cert_pem, secret), time.time())
    a.save()
    a._trust_members()
    members, removed = membership.parse(reply, a.config.group.id)
    c.config.group = Group(a.config.group.id, members, removed=removed)
    c.save()
    c.group_changed()


def connect_all(daemons):
    for x in daemons.values():
        for y in daemons.values():
            if x is not y and y.config.group is not None:
                introduce(x, y)


def test_a_third_virgil_is_trusted_by_everyone(tmp_path):
    """C joins through A. B learns of C from A's push, and then accepts C's heartbeats."""
    paths, identities, configs = make_three(tmp_path)

    async def scenario():
        daemons, runners = await three_running(paths, identities)
        try:
            c_joins_through_a(daemons, identities)
            connect_all(daemons)
            assert configs["C"].id not in daemons["B"].config.group.members  # B doesn't know C yet
            told = await daemons["A"].push_members()
            heartbeat = await daemons["C"]._member_call(configs["B"].id, "POST", "/group/v1/heartbeat",
                                                        daemons["C"].heartbeat().encode())
            return told, daemons
        finally:
            for runner in runners:
                await runner.cleanup()

    told, daemons = asyncio.run(scenario())
    assert "Virgil B" in told
    assert configs["C"].id in daemons["B"].config.group.members  # B learned of C...
    assert configs["C"].id in config_file.load(paths["B"]).group.members  # ...and saved it
    assert configs["C"].id in daemons["B"].peers  # and accepted C's heartbeat


def test_a_member_that_missed_the_news_catches_up_from_a_heartbeat(tmp_path):
    """B was away when C joined; A's next heartbeat carries a different digest, so B fetches A's list."""
    paths, identities, configs = make_three(tmp_path)

    async def scenario():
        daemons, runners = await three_running(paths, identities)
        try:
            c_joins_through_a(daemons, identities)
            connect_all(daemons)
            await daemons["A"]._member_call(configs["B"].id, "POST", "/group/v1/heartbeat",
                                            daemons["A"].heartbeat().encode())
            for _ in range(50):  # B fetches A's list in the background
                if configs["C"].id in daemons["B"].config.group.members:
                    break
                await asyncio.sleep(0.05)
            return daemons
        finally:
            for runner in runners:
                await runner.cleanup()

    daemons = asyncio.run(scenario())
    assert configs["C"].id in daemons["B"].config.group.members
    assert membership.digest(daemons["A"].config.group) == membership.digest(daemons["B"].config.group)


def test_removing_a_member_tells_it_and_everyone(tmp_path):
    paths, identities, configs = make_three(tmp_path)

    async def scenario():
        daemons, runners = await three_running(paths, identities)
        try:
            c_joins_through_a(daemons, identities)
            connect_all(daemons)
            await daemons["A"].push_members()
            daemons["B"].peers[configs["C"].id] = Peer(daemons["C"].heartbeat(), time.monotonic())
            daemons["A"].election.resume(State.LEADER, None, 0.0)  # settled: it can change the member list
            name = await daemons["A"].remove_member(configs["C"].id)
            return name, daemons
        finally:
            for runner in runners:
                await runner.cleanup()

    name, daemons = asyncio.run(scenario())
    assert name == "Virgil C"
    titles = lambda d: [e["title"] for e in d.events.recent]
    assert "Virgil C was removed from the group by Virgil A" in titles(daemons["B"])
    assert "Virgil C was removed from the group by Virgil A" in titles(daemons["A"])
    assert "This Virgil was removed from the group, and carries on alone" in titles(daemons["C"])
    assert daemons["C"].config.group is None  # C was told, and left: it carries on alone
    assert configs["C"].id not in daemons["B"].config.group.members  # B was told too
    assert configs["C"].id in daemons["B"].config.group.removed
    assert configs["C"].id not in daemons["B"].peers  # and forgot it (a removed leader is lost at once)


def test_leaving_the_group(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
        runners = await start(a) + await start(b)
        try:
            introduce(a, b)
            introduce(b, a)
            await b.leave_group()
            return a, b
        finally:
            for runner in runners:
                await runner.cleanup()

    a, b = asyncio.run(scenario())
    assert b.config.group is None and config_file.load(paths["B"]).group is None
    assert "Virgil B left the group" in [e["title"] for e in a.events.recent]
    assert "This Virgil left the group, and carries on alone" in [e["title"] for e in b.events.recent]
    assert configs["B"].id not in a.config.group.members and configs["B"].id in a.config.group.removed


def test_a_virgil_removed_while_away_finds_out(tmp_path):
    """C was offline when A removed it. Hearing nobody, C asks A's public port, and learns it was removed."""
    paths, identities, configs = make_three(tmp_path)

    async def scenario():
        daemons, runners = await three_running(paths, identities)
        try:
            c_joins_through_a(daemons, identities)
            connect_all(daemons)
            membership.remove(daemons["A"].config.group, configs["C"].id, time.time() + 1)  # as if while C was away
            await daemons["C"].check_still_member()
            return daemons["C"].removed_notice
        finally:
            for runner in runners:
                await runner.cleanup()

    assert asyncio.run(scenario()) == "Virgil A says this Virgil was removed from the group"


def test_a_virgil_cant_remove_itself(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    with pytest.raises(RuntimeError, match="leave the group instead"):
        asyncio.run(a.remove_member(configs["A"].id))


def test_a_new_leader_goes_in_the_history(tmp_path):
    from virgil.election import Change, State

    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.timing = object()  # managing its timing, so it isn't just observing
    a._log_change(Change(State.FOLLOWER, State.LEADER, configs["B"].id, None))
    a._log_change(Change(State.LEADER, State.FOLLOWER, None, configs["B"].id))
    assert [e["title"] for e in a.events.recent][:2] == ["Virgil B became the leader", "This Virgil became the leader"]


def test_an_unsettled_virgil_can_leave_but_cant_remove_or_stamp_the_future(tmp_path, monkeypatch):
    """Removing is refused until the clock settles; leaving isn't (a Virgil with a broken clock is just the one that
    might want out), but it's stamped just after the latest change known, not by the broken clock."""
    import virgil.daemon as daemon_module

    paths, identities, configs = make_three(tmp_path)

    async def scenario():
        daemons, runners = await three_running(paths, identities)
        try:
            c_joins_through_a(daemons, identities)
            connect_all(daemons)
            daemons["A"].election.resume(State.LEADER, None, 0.0)
            await daemons["A"].push_members()  # B learns of C
            b = daemons["B"]  # in the group, still listening: not settled
            with pytest.raises(RuntimeError, match="hasn't settled"):
                await b.remove_member(configs["C"].id)
            latest = membership.latest(b.config.group)
            real = time.time
            monkeypatch.setattr(daemon_module.time, "time", lambda: real() + 5 * 365 * 86400)  # years ahead
            await b.leave_group()
            return latest, daemons["A"].config.group  # what the others were told
        finally:
            for runner in runners:
                await runner.cleanup()

    latest, group = asyncio.run(scenario())
    assert configs["B"].id not in group.members  # B left
    assert group.removed[configs["B"].id].changed == pytest.approx(latest + 0.001)  # not five years on
