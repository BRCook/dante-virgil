"""Two virgild instances on this machine, heartbeating and electing a leader."""

import asyncio
import json
import shutil
import ssl
import time
import urllib.request

import aiohttp
import pytest
from aiohttp import web

from virgil import config as config_file
from virgil import discovery, tls
from virgil.daemon import Daemon
from virgil.election import Election, State, Timing
from virgil.generate import Role
from virgil.group import Member, create
from virgil.heartbeat import HEARTBEAT_PATH, Heartbeat
from virgil.timing import Target

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

FAST = Timing(visible=1.5, listen_settle=1.0, listen_timeout=3.0, leader_lost=5.0)


def make_pair(tmp_path):
    """Two Virgils in one group, A and B."""
    paths, identities, configs = {}, {}, {}
    for name in ("A", "B"):
        identity = tls.default_identity(tmp_path / name / "tls")
        tls.generate(identity, hostname=name.lower())
        config = config_file.new_config()
        config.name = f"Virgil {name}"
        paths[name], identities[name], configs[name] = tmp_path / name / "virgil.toml", identity, config
    group = create(configs["A"].id, "Virgil A", identities["A"].cert_pem)
    group.members[configs["B"].id] = Member("Virgil B", identities["B"].cert_pem)
    for name in ("A", "B"):
        configs[name].group = group
        config_file.save(configs[name], paths[name])
    return paths, identities, configs


async def start(daemon):
    runners, ports = [], []
    for app, context in ((daemon.public_app(), tls.public_server_context(daemon.identity)),
                         (daemon.group_app(), daemon.group_context)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        runners.append(runner)
        ports.append(site._server.sockets[0].getsockname()[1])
    daemon.public_port, daemon.group_port = ports
    return runners


def introduce(daemon, other):
    """Tell one daemon where the other is, as mDNS would, and that its network is up."""
    daemon.addresses = ("127.0.0.1",)
    daemon.adverts[other.config.id] = discovery.Advert(
        other.config.id, other.config.name, "0.1.0", other.config.group.id,
        other.public_port, other.group_port, ("127.0.0.1",))


def test_two_daemons_elect_one_leader(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        b = Daemon(paths["B"], identities["B"])
        for daemon in (a, b):
            daemon.election = Election(daemon.config.id, time.monotonic(), timing=FAST)
        runners = await start(a) + await start(b)
        introduce(a, b)
        introduce(b, a)
        tasks = [asyncio.create_task(t) for d in (a, b) for t in (d.send_heartbeats(), d.run_election())]
        try:
            await asyncio.sleep(4)
            status = await asyncio.to_thread(_status, a.public_port)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for runner in runners:
                await runner.cleanup()
        return a, b, status

    a, b, status = asyncio.run(scenario())
    states = {a.election.state, b.election.state}
    assert states == {State.LEADER, State.FOLLOWER}
    leader, follower = (a, b) if a.election.state is State.LEADER else (b, a)
    assert follower.election.leader == leader.config.id

    (member,) = status["group"]["members"]
    assert member["name"] == "Virgil B" and member["heard_ago"] < 2
    assert status["observing"] is True


def _status(port):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(f"https://127.0.0.1:{port}/api/v1/status", context=context) as reply:
        return json.load(reply)


def test_a_member_cant_pretend_to_be_another(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        runners = await start(a)
        try:
            # B's certificate, claiming A's ID.
            context = tls.group_client_context(identities["B"], [identities["A"].cert_pem])
            beat = Heartbeat(configs["A"].id, "Virgil A", "0.1.0", State.LEADER, 1, None, False, None)
            async with aiohttp.ClientSession() as session:
                url = f"https://127.0.0.1:{a.group_port}{HEARTBEAT_PATH}"
                async with session.post(url, json=beat.encode(), ssl=context) as reply:
                    return reply.status, a.peers
        finally:
            for runner in runners:
                await runner.cleanup()

    status, peers = asyncio.run(scenario())
    assert status == 403 and peers == {}


class FakeTiming:
    """Carries out every decision at once, and remembers them."""

    def __init__(self, daemon, applied=None):
        self.daemon = daemon
        self.applied = applied
        self.request = object() if applied else None
        self.wanted = []

    def want(self, target):
        self.wanted.append(target)
        self.applied = target
        self.daemon.on_applied(target)

    def poke(self):
        pass


def test_decisions_reach_the_timing_services(tmp_path):

    paths, identities, configs = make_pair(tmp_path)
    for name in ("A", "B"):
        config = config_file.load(paths[name])
        config.ptp_interface = "enp1s0"
        config_file.save(config, paths[name])

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        b = Daemon(paths["B"], identities["B"])
        for daemon in (a, b):
            daemon.election = Election(daemon.config.id, time.monotonic(), timing=FAST)
            daemon.start_timing(FakeTiming(daemon))
        runners = await start(a) + await start(b)
        introduce(a, b)
        introduce(b, a)
        tasks = [asyncio.create_task(t) for d in (a, b) for t in (d.send_heartbeats(), d.run_election())]
        try:
            await asyncio.sleep(4)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for runner in runners:
                await runner.cleanup()
        return a, b

    a, b = asyncio.run(scenario())
    leader, follower = (a, b) if a.election.state is State.LEADER else (b, a)
    assert leader.timing.wanted == [Target(Role.LEADER)]
    assert follower.timing.wanted == [Target(Role.FOLLOWER, leader.config.id)]
    # The follower only became a follower once its timing services were set up.
    assert follower.election.state is State.FOLLOWER
    assert not follower.observing


def test_restarting_virgild_carries_on_in_the_running_role(tmp_path):

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["B"])
    config.ptp_interface = "enp1s0"
    config_file.save(config, paths["B"])
    b = Daemon(paths["B"], identities["B"])
    b.start_timing(FakeTiming(b, applied=Target(Role.FOLLOWER, configs["A"].id)))
    assert b.election.state is State.FOLLOWER
    assert b.election.leader == configs["A"].id


def test_observe_only_leaves_roles_alone(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.ptp_interface = "enp1s0"
    config.observe_only = True
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    a.start_timing(FakeTiming(a))
    assert a.timing is None and a.observing


def test_listening_leaves_the_timing_services_alone():
    assert Daemon._target(State.LISTEN, None) is False
    assert Daemon._target(State.STOPPED, None) is None


def test_the_election_waits_for_the_network(tmp_path, monkeypatch):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    start = a.started
    assert not a.ready_to_elect(start + 5)
    # The listening countdown starts when the network does, not when virgild did.
    a.addresses = ("10.0.0.1",)
    assert a.ready_to_elect(start + 20)
    assert a.election.since == start + 20


def test_the_election_gives_up_waiting_eventually(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    from virgil.daemon import NETWORK_WAIT

    assert not a.ready_to_elect(a.started + NETWORK_WAIT - 1)
    assert a.ready_to_elect(a.started + NETWORK_WAIT + 0.1)


def test_network_changes_restart_discovery_and_update_timing(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.mgmt_interface = "enp5s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])

    state = {"addresses": [], "networks": []}
    monkeypatch.setattr(daemon_module, "ipv4_addresses", lambda name: state["addresses"])
    monkeypatch.setattr(daemon_module, "ipv4_networks", lambda name: state["networks"])
    started, stopped = [], []

    class FakeDiscovery:
        def __init__(self, on_advert):
            pass

        async def start(self, addresses, *args):
            started.append(addresses)

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr(daemon_module.discovery, "Discovery", FakeDiscovery)

    class Poked:
        count = 0

        def poke(self):
            Poked.count += 1

    a.timing = Poked()

    async def scenario():
        await a.check_network()  # no address yet
        state.update(addresses=["10.0.0.5"], networks=["10.0.0.0/24"])
        await a.check_network()
        await a.check_network()  # unchanged
        state.update(addresses=["10.0.1.5"], networks=["10.0.1.0/24"])
        await a.check_network()

    asyncio.run(scenario())
    assert started == [["10.0.0.5"], ["10.0.1.5"]]
    assert stopped == [True]
    assert Poked.count == 2
    assert a.networks == ("10.0.1.0/24",)


def test_leader_request_uses_the_current_networks(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.ptp_interface = "enp1s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    a.networks = ("10.10.100.0/24",)
    request = a.build_request(Target(Role.LEADER))
    assert request.allow == ("10.10.100.0/24",)
