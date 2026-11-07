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
    monkeypatch.setattr(daemon_module, "ipv4_addresses", lambda name, **kw: state["addresses"])
    monkeypatch.setattr(daemon_module, "ipv4_networks", lambda name, **kw: state["networks"])
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


def test_networks_are_known_before_the_election_can_start(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.mgmt_interface = "enp5s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    monkeypatch.setattr(daemon_module, "ipv4_addresses", lambda name, **kw: ["10.0.0.5"])
    monkeypatch.setattr(daemon_module, "ipv4_networks", lambda name, **kw: ["10.0.0.0/24"])
    seen = []

    class SlowDiscovery:
        def __init__(self, on_advert):
            pass

        async def start(self, *args):
            # The election could run while discovery starts; the networks must already be set.
            seen.append(a.networks)

        async def stop(self):
            pass

    monkeypatch.setattr(daemon_module.discovery, "Discovery", SlowDiscovery)
    asyncio.run(a.check_network())
    assert seen == [("10.0.0.0/24",)]


def test_local_dante_devices_reach_heartbeats_and_status(tmp_path):
    from virgil.ptpwatch import LocalDevice

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.ptp_interface = "enp1s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])

    class Client:
        async def devices(self):
            return {"Wing.local.": {"name": "Wing", "ipv4": "10.10.100.50", "mac_address": "001dc1aabbcc",
                                    "clock_status": {"ptpv2_enabled": True}, "aes67_current": False}}

        async def diagnostics(self, server_name):
            return {"clock": {}}

    a.start_consoles(client=Client())
    a.ptp.devices["10.10.100.50"] = LocalDevice("10.10.100.50", 0.0, ptpv1=True, follows_this_virgil=True)
    asyncio.run(a.consoles.poll())
    (summary,) = a.heartbeat().dante_devices
    assert summary["name"] == "Wing" and summary["follows_this_virgil"] is True and summary["ptpv2"] is True


def _fake_interfaces(monkeypatch, addresses):
    """addresses: a dict the test changes, mapping interface name to its IPv4 addresses with prefixes."""
    import ipaddress

    import virgil.daemon as daemon_module
    from virgil.interfaces import is_link_local

    def ipv4_addresses(name, run=None, link_local=False):
        return [a.split("/")[0] for a in addresses.get(name, []) if link_local or not is_link_local(a.split("/")[0])]

    def ipv4_networks(name, run=None, link_local=False):
        return [str(ipaddress.ip_network(a, strict=False)) for a in addresses.get(name, [])
                if link_local or not is_link_local(a.split("/")[0])]

    monkeypatch.setattr(daemon_module, "ipv4_addresses", ipv4_addresses)
    monkeypatch.setattr(daemon_module, "ipv4_networks", ipv4_networks)

    class QuietDiscovery:
        def __init__(self, on_advert):
            pass

        async def start(self, *args):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(daemon_module.discovery, "Discovery", QuietDiscovery)


def test_a_link_local_address_counts_once_dhcp_has_had_its_chance(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.mgmt_interface = "enp5s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    addresses = {"enp5s0": ["169.254.13.123/16"]}
    _fake_interfaces(monkeypatch, addresses)
    clock = [1000.0]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: clock[0])

    asyncio.run(a.check_network())
    assert a.addresses == ()  # DHCP might still come
    clock[0] += daemon_module.LINK_LOCAL_GRACE + 1
    asyncio.run(a.check_network())
    assert a.addresses == ("169.254.13.123",) and a.networks == ("169.254.0.0/16",)

    # DHCP turning up later takes over.
    addresses["enp5s0"] = ["169.254.13.123/16", "10.10.100.251/24"]
    asyncio.run(a.check_network())
    assert a.addresses == ("10.10.100.251",) and a.networks == ("10.10.100.0/24",)


def test_netaudio_runs_only_while_the_ptp_interface_has_an_address(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["B"])
    config.ptp_interface = "enp1s0"
    config_file.save(config, paths["B"])
    b = Daemon(paths["B"], identities["B"])
    addresses = {"enp1s0": []}
    _fake_interfaces(monkeypatch, addresses)
    monkeypatch.setattr(daemon_module, "NETAUDIO_ENV", tmp_path / "netaudio.env")

    class Systemd:
        calls = []

        def restart(self, unit, block=True):
            self.calls.append(("restart", unit))

        def stop(self, unit):
            self.calls.append(("stop", unit))

    systemd = Systemd()
    b.systemd = systemd
    b.check_ptp_interface()
    # Nothing plugged in: stopped, in case an older virgild left it retrying.
    assert systemd.calls == [("stop", "dante-virgil-netaudio.service")]
    b.check_ptp_interface()
    assert len(systemd.calls) == 1
    addresses["enp1s0"] = ["169.254.40.2/16"]  # a link-local Dante network counts at once
    b.check_ptp_interface()
    b.check_ptp_interface()
    assert systemd.calls[1:] == [("restart", "dante-virgil-netaudio.service")]
    assert (tmp_path / "netaudio.env").read_text() == "NETAUDIO_INTERFACE=enp1s0\n"
    addresses["enp1s0"] = []
    b.check_ptp_interface()
    assert systemd.calls[-1] == ("stop", "dante-virgil-netaudio.service")


def test_netaudio_stopped_on_purpose_isnt_a_problem(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])
    assert b.netaudio_running  # without systemd (as here), assumed running
    b.systemd = object()
    b.netaudio_for = None
    assert not b.netaudio_running
    b.netaudio_for = ("10.10.100.211",)
    assert b.netaudio_running


def test_hand_over_between_two_daemons(tmp_path):
    """B, a follower, asks A, the leader, to hand over; a stranger to the request can't."""
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        b = Daemon(paths["B"], identities["B"])
        runners = await start(a) + await start(b)
        introduce(a, b)
        introduce(b, a)
        now = time.monotonic()
        a.election.resume(State.LEADER, None, now)
        b.election.resume(State.FOLLOWER, a.config.id, now)
        a.timing = FakeTiming(a)
        b.timing = FakeTiming(b)
        try:
            refused = await a.request_lead()  # a leader asking to lead
            status, body = await b.request_lead()
            return refused, status, body, a.election.handover_to
        finally:
            for runner in runners:
                await runner.cleanup()

    refused, status, body, handover_to = asyncio.run(scenario())
    assert refused == (200, {"message": "Virgil A is already leading"})
    assert status == 202 and "handing leadership over" in body["message"]
    assert handover_to == configs["B"].id


def test_a_member_can_only_ask_to_lead_itself(tmp_path):
    from virgil.control import HANDOVER_PATH

    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        a.election.resume(State.LEADER, None, time.monotonic())
        runners = await start(a)
        try:
            context = tls.group_client_context(identities["B"], [identities["A"].cert_pem])
            async with aiohttp.ClientSession() as session:
                url = f"https://127.0.0.1:{a.group_port}{HANDOVER_PATH}"
                async with session.post(url, json={"to": configs["A"].id}, ssl=context) as reply:
                    return reply.status, a.election.handover_to
        finally:
            for runner in runners:
                await runner.cleanup()

    status, handover_to = asyncio.run(scenario())
    assert status == 403 and handover_to is None


def test_only_a_follower_can_ask_to_lead(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])
    b.timing = FakeTiming(b)
    status, body = asyncio.run(b.request_lead())  # still listening
    assert status == 409 and "only take the lead as a follower" in body["error"]
    b.timing = None
    status, body = asyncio.run(b.request_lead())
    assert status == 409 and "observing" in body["error"]


def test_the_advert_has_a_host_name_of_its_own(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.mgmt_interface = "enp5s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    monkeypatch.setattr(daemon_module, "ipv4_addresses", lambda name, **kw: ["10.10.100.253"])
    monkeypatch.setattr(daemon_module, "ipv4_networks", lambda name, **kw: ["10.10.100.0/24"])
    hosts = []

    class Recording:
        def __init__(self, on_advert):
            pass

        async def start(self, addresses, instance, host, port, props):
            hosts.append((addresses, host))

        async def stop(self):
            pass

    monkeypatch.setattr(daemon_module.discovery, "Discovery", Recording)
    asyncio.run(a.check_network())
    assert hosts == [(["10.10.100.253"], f"virgil-{configs['A'].id[:12]}")]
