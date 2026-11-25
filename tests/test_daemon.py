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


def test_netaudio_follows_the_ptp_interface_and_falls_back_to_management(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["B"])
    config.ptp_interface, config.mgmt_interface = "enp1s0", "enp5s0"
    config_file.save(config, paths["B"])
    b = Daemon(paths["B"], identities["B"])
    addresses = {"enp1s0": [], "enp5s0": []}
    _fake_interfaces(monkeypatch, addresses)
    monkeypatch.setattr(daemon_module, "NETAUDIO_ENV", tmp_path / "netaudio.env")

    class Systemd:
        calls = []

        def restart(self, unit, block=True):
            self.calls.append(("restart", (tmp_path / "netaudio.env").read_text().strip()))

        def stop(self, unit):
            self.calls.append(("stop", None))

    systemd = Systemd()
    b.systemd = systemd
    b.check_ptp_interface()
    assert systemd.calls == [("stop", None)]  # no addresses anywhere yet
    addresses["enp5s0"] = ["192.168.30.15/24"]
    b.check_ptp_interface()
    assert systemd.calls[-1] == ("restart", "NETAUDIO_INTERFACE=enp5s0")  # its UI still works
    assert b.netaudio_interface == "enp5s0"
    addresses["enp1s0"] = ["169.254.40.2/16"]  # a link-local Dante network counts at once
    b.check_ptp_interface()
    b.check_ptp_interface()
    assert systemd.calls[-1] == ("restart", "NETAUDIO_INTERFACE=enp1s0") and len(systemd.calls) == 3
    addresses["enp1s0"] = []
    b.check_ptp_interface()
    assert systemd.calls[-1] == ("restart", "NETAUDIO_INTERFACE=enp5s0")


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


def test_taking_the_lead_adopts_the_phc_rate_as_the_trim(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])
    b.clock = {"phc_adjustment_ppm": 22.345}
    b.adopt_phc_rate()
    assert b.config.trim_ppb == 22345
    assert config_file.load(paths["B"]).trim_ppb == 22345
    b.clock = None
    b.adopt_phc_rate()  # nothing known: leave it
    assert b.config.trim_ppb == 22345


def _leading_with_consoles(tmp_path, offsets, auto_trim=True):
    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.trim_ppb = 600
    config.auto_trim = auto_trim
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    a.election.resume(State.LEADER, None, -400.0)  # leading for long enough to have readings to steer by

    class Timing:
        """Applies every trim at once, unless told the trim unit is failing."""

        pokes = 0
        failing = False
        request = None

        def poke(self):
            Timing.pokes += 1
            if not Timing.failing:
                Timing.request = replace(Timing.request, trim_ppb=a.config.trim_ppb)

    from dataclasses import replace

    from virgil.generate import Role as R
    from virgil.request import TimingRequest

    Timing.request = TimingRequest(R.LEADER, "enp1s0", None, (), a.config.trim_ppb)
    a.timing = Timing()
    beat = Heartbeat(configs["B"].id, "Virgil B", "0.1.0", State.FOLLOWER, 1, configs["A"].id, False, None,
                     dante_devices=tuple({"name": f"Wing {i}", "follows_this_virgil": True, "clock_offset_ppb": o}
                                         for i, o in enumerate(offsets)),
                     clock={"rms_offset_ns": 6089})
    from virgil.daemon import Peer

    a.peers[configs["B"].id] = Peer(beat, 100.0)
    step = a.trim_once
    fed = [None]

    def trim_once(now):
        """As virgild runs it: the devices' readings every five seconds before each step, then the step."""
        start = int(now) - 305 if fed[0] is None else max(fed[0] + 5, int(now) - 305)
        for t in range(start, int(now), 5):
            a.trim_readings.add(float(t), a.console_readings(now), a.config.trim_ppb)
        fed[0] = int(now) - 1
        step(now)

    a.trim_once = trim_once
    return a, paths["A"], Timing


def test_auto_trim_slows_a_fast_leader(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [4750])
    a.trim_once(101.0)
    assert a.config.trim_ppb == 300  # 600 - 300: 4.75 ppm out, so a bigger step, but no more than 0.3 ppm
    assert config_file.load(path).trim_ppb == 300
    assert timing.pokes == 1
    assert a.last_trim["from_ppb"] == 600 and a.last_trim["consoles_ppb"] == 4750


def test_auto_trim_leaves_a_good_trim_alone(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [150])
    a.trim_once(101.0)
    assert a.config.trim_ppb == 600 and timing.pokes == 0


def test_auto_trim_off(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [4750], auto_trim=False)
    a.trim_once(101.0)
    assert a.config.trim_ppb == 600


def test_auto_trim_only_on_a_settled_leader(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [4750])
    a.election.resume(State.LEADER, None, 0.0)
    a.trim_once(10.0)  # took the lead at 0; still settling
    assert a.config.trim_ppb == 600
    a.election.resume(State.FOLLOWER, "f" * 32, 0.0)
    a.trim_once(101.0)
    assert a.config.trim_ppb == 600


def test_auto_trim_ignores_members_not_heard_from(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [4750])
    a.trim_once(200.0)  # B last heard at 100
    assert a.config.trim_ppb == 600


def test_auto_trim_waits_for_each_step_to_take_effect(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [4750])
    timing.failing = True  # as when phc_ctl was refusing negative trims
    a.trim_once(101.0)
    assert a.config.trim_ppb == 300  # 4.75 ppm out: a 0.3 ppm step
    a.trim_once(161.0)
    a.trim_once(221.0)
    assert a.config.trim_ppb == 300  # no further steps until the first is applied


def test_auto_trim_pauses_when_the_consoles_dont_respond(tmp_path, caplog):
    a, path, timing = _leading_with_consoles(tmp_path, [5371])
    (peer,) = a.peers.values()

    def step_at(t):
        peer.heard_at = t - 0.5  # B keeps heartbeating
        a.trim_once(t)

    for step in range(12):
        step_at(141.0 + 140 * step)  # the consoles never move
        if a.trim_paused:
            break  # bigger steps while far out get the trim 1 ppm away sooner
    assert a.trim_paused and "consoles only" in a.trim_paused
    paused_at, when = a.config.trim_ppb, a.trim_paused_at
    step_at(when + 300)
    assert a.config.trim_ppb == paused_at
    assert "Auto-trim paused" in caplog.text
    # Turning it off and on again resumes it.
    a.config.auto_trim = False
    step_at(when + 440)
    a.config.auto_trim = True
    step_at(when + 580)
    assert a.trim_paused is None and a.config.trim_ppb == paused_at - 300  # still far out: a 0.3 ppm step


def test_a_requested_restart(tmp_path):
    from virgil.daemon import RESTART_EXIT

    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])

    async def scenario():
        a.request_restart(delay=0.01)
        await asyncio.wait_for(a.restart.wait(), 1)

    asyncio.run(scenario())
    assert a.restart.is_set() and RESTART_EXIT == 75


def test_when_chrony_gets_hardware_timestamps(tmp_path):
    from tests.test_setup import HARDWARE, INTERFACES
    from virgil.interfaces import Interface, TimestampingInfo

    interfaces = dict(INTERFACES)
    # A second port on the i226's card, sharing its hardware clock (/dev/ptp0).
    interfaces["enp1s0d1"] = Interface("enp1s0d1", "ff", "igc", True, True, TimestampingInfo(HARDWARE, 0, 3, 3))
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    lookup = interfaces.get
    c = a.config
    c.ptp_interface, c.mgmt_interface = "enp1s0", "enp5s0"
    assert a.ntp_hwtimestamp_interface(lookup) is None  # Realtek management: software only
    c.mgmt_interface = "enp1s0"
    assert a.ntp_hwtimestamp_interface(lookup) is None  # the PTP interface itself: never shared
    c.mgmt_interface = "enp1s0d1"
    assert a.ntp_hwtimestamp_interface(lookup) is None  # same card, same hardware clock: never shared
    c.mgmt_interface = "enp2s0"
    assert a.ntp_hwtimestamp_interface(lookup) == "enp2s0"  # a separate hardware NIC (/dev/ptp1)
    c.ntp_hwtimestamp = False
    assert a.ntp_hwtimestamp_interface(lookup) is None


def test_dhcp_names_are_set_on_start_and_on_change(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.manage_timing = True
    a.config.ptp_interface, a.config.mgmt_interface = "enp1s0", "enp5s0"
    started = []

    class Systemd:
        def start(self, unit, block=True):
            started.append(unit)

    request = tmp_path / "dhcp-names.json"
    a.update_dhcp_names(Systemd(), request)
    a.update_dhcp_names(Systemd(), request)  # nothing changed
    assert started == ["dante-virgil-dhcpnames.service"]
    assert json.loads(request.read_text()) == {"ptp_interface": "enp1s0", "mgmt_interface": "enp5s0", "add": []}
    a.config.mgmt_interface = "enp2s0"
    a.update_dhcp_names(Systemd(), request)
    assert len(started) == 2


def test_status_shows_the_dhcp_names(tmp_path):
    import socket

    from virgil.daemon import dhcp_lease_names

    names = dhcp_lease_names()
    assert names["ptp"].endswith("-ptp") and names["mgmt"].endswith("-mgmt")
    assert names["ptp"].startswith(socket.gethostname().split(".")[0].lower()[:10])


def _paused(tmp_path):
    a, path, timing = _leading_with_consoles(tmp_path, [5371])
    (peer,) = a.peers.values()

    def step_at(t, devices=None):
        peer.heard_at = t - 0.5
        if devices is not None:
            from dataclasses import replace

            peer.heartbeat = replace(peer.heartbeat, dante_devices=tuple(devices))
        a.trim_once(t)

    for step in range(12):
        step_at(141.0 + 140 * step)
        if a.trim_paused:
            break  # bigger steps while far out get the trim 1 ppm away sooner
    assert a.trim_paused
    return a, step_at


def test_a_pause_ends_by_itself(tmp_path):
    a, step_at = _paused(tmp_path)
    paused_at, trim = a.trim_paused_at, a.config.trim_ppb
    step_at(paused_at + 300)
    assert a.trim_paused and a.config.trim_ppb == trim  # still paused after 5 minutes
    step_at(paused_at + 601)
    assert a.trim_paused is None and a.config.trim_ppb == trim - 300  # resumed, and stepping again (0.3: far out)


def test_a_pause_ends_when_the_consoles_change(tmp_path):
    a, step_at = _paused(tmp_path)
    trim = a.config.trim_ppb
    both = [{"name": "Wing 0", "follows_this_virgil": True, "clock_offset_ppb": 5371},
            {"name": "Wing Rack", "follows_this_virgil": True, "clock_offset_ppb": 2000}]
    step_at(a.trim_paused_at + 140, both)  # a second console starts following
    assert a.trim_paused is None and a.config.trim_ppb == trim  # resumed, but waiting for the newcomer's readings
    step_at(a.trim_paused_at + 140 + 310, both)  # five minutes of them
    assert a.config.trim_ppb < trim


def test_unreachable_netaudio_drops_old_readings():
    import asyncio

    from virgil.consoles import Consoles, DanteDevice, NetaudioClient

    consoles = Consoles(NetaudioClient("http://127.0.0.1:9", timeout=1), lambda: {}, lambda: {})
    consoles.devices = {"10.0.0.5": DanteDevice("Wing.local.", "Wing", "10.0.0.5", None, None, True, False, 5000)}
    asyncio.run(consoles.poll())
    assert consoles.devices == {} and consoles.summaries() == []


def test_alerts_reach_heartbeats_and_status(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.ptp_interface = "enp1s0"
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    a.events_path = tmp_path / "events.jsonl"

    class Client:
        async def devices(self):
            return {"Wing.local.": {"name": "Wing", "ipv4": "10.0.0.5", "clock_status": {"ptpv2_enabled": True}}}

        async def diagnostics(self, server_name):
            return {}

        async def issues(self):
            return {"issues": [{"issue_id": "1", "kind": "clock_muted", "severity": "error", "state": "open",
                                "first_seen": "2026-11-07T21:05:03Z", "title": "Audio muted by clock state",
                                "summary": "", "scope": {"device_name": "Wing", "server_name": "Wing.local."}}]}

    from virgil.ptpwatch import LocalDevice

    a.start_consoles(client=Client())
    a.ptp.devices["10.0.0.5"] = LocalDevice("10.0.0.5", 0.0, ptpv1=True, follows_this_virgil=True)
    asyncio.run(a.consoles.poll())
    asyncio.run(a.alerts.poll())
    (alert,) = a.heartbeat().alerts
    assert alert["label"] == "AUDIO MUTED" and alert["device"] == "Wing"


def _watching(tmp_path, monkeypatch, link_up=True, addresses=("192.168.20.212",)):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.config.ptp_interface = "enp1s0"
    a.ptp_watcher = object()
    a.ptp_watch_started = 1000.0
    a.ptp_addresses = tuple(addresses)
    monkeypatch.setattr(a, "_ptp_link_up", lambda: link_up)
    return a


def test_no_ptp_heard(tmp_path, monkeypatch):
    a = _watching(tmp_path, monkeypatch)
    assert a.ptp_warnings(now=1030.0) == []
    (warning,) = a.ptp_warnings(now=1075.0)
    assert warning.startswith("No PTP has been heard on enp1s0 for 75 s") and "multicast" in warning
    a.ptp.last_heard = 1070.0
    assert a.ptp_warnings(now=1075.0) == []


def test_ptp4l_without_an_address(tmp_path, monkeypatch):
    a = _watching(tmp_path, monkeypatch, addresses=())
    (warning,) = a.ptp_warnings(now=1001.0)
    assert "enp1s0 has no IPv4 address, so ptp4l can't send PTP" in warning


def test_nothing_plugged_in_needs_no_warning(tmp_path, monkeypatch):
    a = _watching(tmp_path, monkeypatch, link_up=False, addresses=())
    assert a.ptp_warnings(now=5000.0) == []


def test_auto_trim_waits_for_an_unsettled_follower(tmp_path):
    from dataclasses import replace

    a, path, timing = _leading_with_consoles(tmp_path, [4750])
    (peer,) = a.peers.values()
    peer.heartbeat = replace(peer.heartbeat, clock={"rms_offset_ns": 45_000})
    a.trim_once(101.0)
    assert a.config.trim_ppb == 600  # still catching up: wait
    peer.heartbeat = replace(peer.heartbeat, clock={"rms_offset_ns": 6_000})
    a.trim_once(101.5)
    assert a.config.trim_ppb == 300


def test_netaudios_ui_follows_the_management_addresses(tmp_path, monkeypatch):
    """Only management addresses and loopback; never the PTP interface's."""
    import socket

    import virgil.daemon as daemon_module

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(daemon_module, "NETAUDIO_PROXY_PORT", port)
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])

    def reachable(address):
        with socket.socket() as s:
            s.settimeout(1)
            return s.connect_ex((address, port)) == 0

    async def scenario():
        async def page(request):
            return web.Response(text="netaudio")

        app = web.Application()
        app.router.add_get("/", page)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        a.netaudio_ui = runner
        try:
            a.addresses = ("127.0.0.2",)  # the management interface
            await a.bind_netaudio_ui()
            first = {addr: reachable(addr) for addr in ("127.0.0.1", "127.0.0.2", "127.0.0.3")}
            a.addresses = ("127.0.0.3",)  # it moved
            await a.bind_netaudio_ui()
            second = {addr: reachable(addr) for addr in ("127.0.0.1", "127.0.0.2", "127.0.0.3")}
            return first, second
        finally:
            await runner.cleanup()

    first, second = asyncio.run(scenario())
    assert first == {"127.0.0.1": True, "127.0.0.2": True, "127.0.0.3": False}
    assert second == {"127.0.0.1": True, "127.0.0.2": False, "127.0.0.3": True}


def test_graphs_record_this_virgils_own_figures(tmp_path):
    from virgil.consoles import DanteDevice

    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])
    b.election.resume(State.FOLLOWER, configs["A"].id, 0.0)
    b.clock = {"system_offset_ns": 54, "phc_offset_ns": 2220}

    class Consoles:
        late = 100

        def summaries(self):
            return [{"name": "WingRack", "clock_offset_ppb": -849, "latency_setting_ns": 5_000_000,
                     "latency_ns": 2_708_333.3, "latency_average_ns": 1_400_000.0, "late_packets": self.late}]

    consoles = Consoles()
    consoles.polls = 1
    b.consoles = consoles
    b.sample_graphs(1000.0)
    b.sample_graphs(1001.0)  # nothing new read: nothing recorded again
    assert len(b.graphs.series["offset/Virgil B"].points) == 1
    assert len(b.graphs.series["console/Virgil B/WingRack"].points) == 1
    consoles.late, consoles.polls = 130, 2
    b.clock = dict(b.clock)
    b.sample_graphs(1002.0)
    keys = set(b.graphs.series)
    assert {"offset/Virgil B", "phc/Virgil B", "console/Virgil B/WingRack", "limit/Virgil B/WingRack",
            "latency/Virgil B/WingRack", "average/Virgil B/WingRack", "late/Virgil B/WingRack"} <= keys
    assert list(b.graphs.series["late/Virgil B/WingRack"].points) == [(1002.0, 15.0)]  # 30 late in 2 s
    assert b.graphs.series["console/Virgil B/WingRack"].points[-1][1] == -0.849

    # netaudio's own history takes over the latency lines, at its times.
    device = DanteDevice("WingRack.local.", "WingRack", "192.168.20.246", None, None, True, False, -849)
    wall_now = time.time()
    b.on_latency_history(device, [(wall_now - 3, 2_000_000.0, 1_500_000.0),
                                  (wall_now - 2, 5_600_000.0, 5_100_000.0)])
    points = list(b.graphs.series["latency/Virgil B/WingRack"].points)
    assert points[-1][1] == 5_600_000.0 and abs(points[-1][0] - (time.time() - 2)) < 0.5
    recent = lambda: [p for p in b.graphs.series["latency/Virgil B/WingRack"].points if p[0] > time.time() - 60]
    before = len(recent())
    b.sample_graphs(time.time())
    assert len(recent()) == before  # no repeats from the fallback once netaudio's history arrives

def test_pings_become_round_trip_jitter_and_loss(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.member_name_at = lambda address: "Virgil B"
    for rtt in (1_000_000, 1_200_000, None, 1_100_000):
        a.on_ping("10.0.0.2", rtt)
    assert len(a.graphs.series["rtt/Virgil A/Virgil B"].points) == 3
    assert a.graphs.series["rtt/Virgil A/Virgil B"].label == "Virgil A ↔ Virgil B (Mgmt)"
    assert [v for _, v in a.graphs.series["loss/Virgil A/Virgil B"].points] == [0.0, 0.0, 100.0, 0.0]
    assert a.graphs.series["jitter/Virgil A/Virgil B"].points[-1][1] > 0


def test_the_graphs_api(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        a.graphs.add("offset/Virgil B", "offset", "Virgil B", "ns", time.time() - 5, 54)
        runners = await start(a)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://127.0.0.1:{a.public_port}/api/v1/graphs?window=60"
                async with session.get(url, ssl=False) as reply:
                    good = await reply.json()
                async with session.get(f"https://127.0.0.1:{a.public_port}/api/v1/graphs?window=lots", ssl=False) as reply:
                    bad = reply.status
            return good, bad
        finally:
            for runner in runners:
                await runner.cleanup()

    good, bad = asyncio.run(scenario())
    assert good["window"] == 60 and good["series"][0]["label"] == "Virgil B" and bad == 400


def test_the_page_gets_every_members_graphs(tmp_path):
    """Virgil A's page shows Virgil B's figures, fetched from B over the group connection."""
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        b = Daemon(paths["B"], identities["B"])
        runners = await start(a) + await start(b)
        introduce(a, b)
        introduce(b, a)
        from virgil.daemon import Peer

        a.peers[configs["B"].id] = Peer(b.heartbeat(), time.monotonic())  # A has heard from B
        now = time.time()
        a.graphs.add("trim/Virgil A", "trim", "Virgil A", "ppm", now - 5, -5.35)
        b.graphs.add("offset/Virgil B", "offset", "Virgil B", "ns", now - 5, 54)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://127.0.0.1:{a.public_port}/api/v1/graphs?window=60"
                async with session.get(url, ssl=False) as reply:
                    return await reply.json()
        finally:
            for runner in runners:
                await runner.cleanup()

    merged = asyncio.run(scenario())
    assert {s["key"] for s in merged["series"]} == {"trim/Virgil A", "offset/Virgil B"}


def test_a_group_recording_comes_back_as_one_file(tmp_path):
    """Started on A's page: B records too, and stopping on A bundles both into one file."""
    import tarfile

    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        b = Daemon(paths["B"], identities["B"])
        a.recordings_root, b.recordings_root = tmp_path / "rec-a", tmp_path / "rec-b"
        runners = await start(a) + await start(b)
        a.recording.units = b.recording.units = None  # no journal in tests
        introduce(a, b)
        introduce(b, a)
        from virgil.daemon import Peer

        a.peers[configs["B"].id] = Peer(b.heartbeat(), time.monotonic())
        try:
            started = await a.start_recording(group=True)
            assert b.recording.active and started["members"] == ["Virgil B"]
            await asyncio.sleep(0.1)
            stopped = await a.stop_recording()
            return stopped, a, b
        finally:
            for runner in runners:
                await runner.cleanup()

    stopped, a, b = asyncio.run(scenario())
    bundle = tmp_path / "rec-a" / stopped["archive"]
    assert bundle.name.endswith("-group.tar")
    with tarfile.open(bundle) as tar:
        inner = sorted(tar.getnames())
        group = json.loads(tar.extractfile("summary.json").read())
        page = tar.extractfile("summary.html").read().decode()
    logs = [name for name in inner if name.endswith(".tar.gz")]
    assert len(logs) == 2 and logs[0].endswith("-Virgil-A.tar.gz") and logs[1].endswith("-Virgil-B.tar.gz")
    assert {"summary.json", "summary.html"} <= set(inner)  # the group's summary, at the top
    assert [s["virgil"] for s in group["virgils"]] == ["Virgil A", "Virgil B"] and "Virgil B" in page
    assert a.recording.summary(bundle.name) == group  # and beside it, for the web UI
    assert not b.recording.active and b.recording.recordings() == []  # B's copy was fetched, then tidied away
    assert [r["name"] for r in a.recording.recordings()] == [bundle.name]


def _time_pair(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    b = Daemon(paths["B"], identities["B"])

    class Timing:
        def __init__(self):
            self.resyncs = 0

        def resync(self):
            self.resyncs += 1

    a.timing, b.timing = Timing(), Timing()
    return a, b, configs


def test_setting_the_groups_time_on_the_leader(tmp_path, monkeypatch):
    import virgil.daemon as daemon_module
    from virgil.daemon import Peer

    a, b, configs = _time_pair(tmp_path)
    monkeypatch.setattr(daemon_module, "SETTIME_REQUEST", tmp_path / "settime.json")
    started = []

    class Systemd:
        def start(self, unit, block=True):
            started.append((unit, json.loads((tmp_path / "settime.json").read_text())))

    async def scenario():
        runners = await start(a) + await start(b)
        introduce(a, b)
        introduce(b, a)
        a.election.resume(State.LEADER, None, time.monotonic())
        b.election.resume(State.FOLLOWER, configs["A"].id, time.monotonic())
        a.peers[configs["B"].id] = Peer(b.heartbeat(), time.monotonic())
        try:
            return await a.set_group_time(-192.5, Systemd())
        finally:
            for runner in runners:
                await runner.cleanup()

    result = asyncio.run(scenario())
    assert started == [("dante-virgil-settime.service", {"delta": -192.5})]
    assert result == {"moved_by": -192.5, "leader": "Virgil A", "resynchronised": ["Virgil B"]}
    assert b.timing.resyncs == 1 and a.timing.resyncs == 0


def test_a_followers_page_asks_the_leader(tmp_path):
    a, b, configs = _time_pair(tmp_path)
    asked = []

    async def scenario():
        b.election.resume(State.FOLLOWER, configs["A"].id, time.monotonic())

        async def call(virgil_id, method, path, body=None, **kwargs):
            asked.append((virgil_id, path, body))
            return {"moved_by": body["delta"], "leader": "Virgil A", "resynchronised": ["Virgil B"]}

        b._member_call = call
        return await b.set_group_time(30.0)

    result = asyncio.run(scenario())
    assert asked == [(configs["A"].id, "/group/v1/settime", {"delta": 30.0})] and result["leader"] == "Virgil A"


def test_no_time_setting_without_managing_timing(tmp_path):
    a, b, configs = _time_pair(tmp_path)
    a.timing = None
    with pytest.raises(RuntimeError, match="observe_only"):
        asyncio.run(a.set_group_time(5.0))


class _FakeServer:
    started = 0

    def __init__(self, interface, address, mac, hardware_tx=True):
        self.interface, self.address, self.mac = interface, address, mac
        self.hardware_tx = hardware_tx
        self.running = False

    def set_hardware_tx(self, hardware_tx):
        self.hardware_tx = hardware_tx

    def start(self):
        _FakeServer.started += 1
        self.running = True

    async def stop(self):
        self.running = False

    def timestamps(self):
        return {"send": "hardware" if self.hardware_tx else "software", "receive": "hardware"}

    def current_followers(self):
        return []


def _ptpv1_virgil(tmp_path, monkeypatch, mode="fallback"):
    import virgil.daemon as daemon_module

    monkeypatch.setattr(daemon_module.ptpv1_module, "Server", _FakeServer)
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.config.ptp_interface, a.config.ptpv1 = "enp1s0", mode
    a.ptp_watcher = object()
    a.ptp_watch_started = 0.0
    a.ptp_addresses = ("192.168.20.212",)
    a.ptp.own_identity = "8c:a6:82:ff:fe:71:3b:b6"
    a.ptp.own_announced_at = 100.0
    return a


def _ptpv1_device(a, address="192.168.20.246", t=100.0):
    from virgil.ptpwatch import LocalDevice

    a.ptp.devices[address] = LocalDevice(address, t, ptpv1=True, v1_leading_at=t)


def test_ptpv1_waits_then_serves_where_nothing_bridges(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch)
    _ptpv1_device(a)
    assert a.ptpv1_wanted(10.0) == (False, "watching the network before deciding")
    wanted, reason = a.ptpv1_wanted(100.0)
    assert wanted and reason == "no device here bridges this Virgil's PTPv2 into PTPv1"

    def alive(t):  # the device and ptp4l keep transmitting
        a.ptp.devices["192.168.20.246"].last_seen = t
        a.ptp.own_announced_at = t

    async def scenario():
        await a.check_ptpv1(100.0)
        first = a.ptpv1_server
        alive(114.0)
        await a.check_ptpv1(114.0)  # not held for 15 s yet
        second = a.ptpv1_server
        alive(116.0)
        await a.check_ptpv1(116.0)
        return first, second, a.ptpv1_server

    first, second, third = asyncio.run(scenario())
    assert first is None and second is None and third is not None and third.running
    assert third.mac == "8c:a6:82:71:3b:b6" and third.address == "192.168.20.212"
    assert a.serving(116.0)["v1"] is True


def test_ptpv1_stands_by_for_a_bridging_device(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch)
    _ptpv1_device(a, "192.168.20.224")
    a.ptp.devices["192.168.20.224"].v1_bridging_at = 100.0
    wanted, reason = a.ptpv1_wanted(100.0)
    assert not wanted and reason == "192.168.20.224 is bridging this Virgil's PTPv2 into PTPv1"


def test_ptpv1_stands_by_for_a_console_that_could_bridge(tmp_path, monkeypatch):
    """While Virgil serves, a console with PTPv2 on goes passive; the console's path is steadier, so step aside."""
    from virgil.consoles import DanteDevice

    a = _ptpv1_virgil(tmp_path, monkeypatch)
    _ptpv1_device(a, "192.168.20.224")

    class Consoles:
        devices = {"192.168.20.224": DanteDevice("Wing.local.", "WingCompact", "192.168.20.224", None, None,
                                                 True, False, 0)}

    a.consoles = Consoles()
    wanted, reason = a.ptpv1_wanted(100.0)
    assert not wanted and reason == "WingCompact can bridge this Virgil's PTPv2 into PTPv1"


@pytest.mark.parametrize("change, reason", [
    (lambda a: setattr(a.config, "ptpv1", "off"), "off in this Virgil's settings"),
    (lambda a: setattr(a, "ptp_addresses", ()), "enp1s0 has no IPv4 address"),
    (lambda a: setattr(a.ptp, "own_announced_at", 50.0), "this Virgil isn't serving PTPv2, so there's no clock to serve"),
    (lambda a: a.ptp.devices.clear(), "no PTPv1 devices here"),
])
def test_ptpv1_standing_by(tmp_path, monkeypatch, change, reason):
    a = _ptpv1_virgil(tmp_path, monkeypatch)
    _ptpv1_device(a)
    change(a)
    assert a.ptpv1_wanted(100.0) == (False, reason)


def test_ptpv1_always(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="always")
    assert a.ptpv1_wanted(1.0) == (True, "always on in this Virgil's settings")  # no waiting, no devices needed


def test_serving_reaches_heartbeats(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch)
    a.ptp.own_announced_at = time.monotonic()
    beat = a.heartbeat()
    assert beat.serving["v2"] is True and beat.serving["v1"] is False and beat.serving["v1_reason"]


def test_changing_a_members_console_from_this_virgils_page(tmp_path):
    """A's page ticks "Enforce native PTPv2" for B's console: B applies and saves it, as if on its own page."""
    from virgil.daemon import Peer
    from virgil.webapi import WebApi

    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        b = Daemon(paths["B"], identities["B"])
        runners = await start(a) + await start(b)
        introduce(a, b)
        introduce(b, a)
        a.peers[configs["B"].id] = Peer(b.heartbeat(), time.monotonic())
        try:
            web_api = WebApi(a)

            class Request:
                pass

            forwarded = await web_api.device(Request(), {"mac": "00:1D:C1:9F:77:FA", "setting": "ptpv2",
                                                         "enabled": True, "virgil": configs["B"].id})
            stranger = await web_api.device(Request(), {"mac": "00:1d:c1:9f:77:fa", "setting": "ptpv2",
                                                        "enabled": True, "virgil": "0" * 32})
            return json.loads(forwarded.body), forwarded.status, stranger.status, a, b
        finally:
            for runner in runners:
                await runner.cleanup()

    forwarded, status, stranger, a, b = asyncio.run(scenario())
    assert status == 200 and forwarded == {"mac": "00:1d:c1:9f:77:fa", "native_ptpv2": True, "aes67": False}
    assert b.config.devices["00:1d:c1:9f:77:fa"].native_ptpv2  # changed on B, where the console is
    assert config_file.load(paths["B"]).devices["00:1d:c1:9f:77:fa"].native_ptpv2  # and saved there
    assert "00:1d:c1:9f:77:fa" not in a.config.devices  # not on A
    assert stranger == 404


def test_heartbeats_carry_each_virgils_console_settings(tmp_path):
    from virgil.config import DeviceSettings
    from virgil.consoles import DanteDevice

    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])
    b.config.devices["00:1d:c1:9f:77:fa"] = DeviceSettings(native_ptpv2=True)

    class Consoles:
        devices = {"192.168.20.246": DanteDevice("WingRack.local.", "WingRack", "192.168.20.246",
                                                 "00:1d:c1:9f:77:fa", None, True, False, 0)}

        def summaries(self):
            return []

        def announcing_themselves(self):
            return set()

    b.consoles = Consoles()
    assert b.heartbeat().device_settings == {"00:1d:c1:9f:77:fa": {"native_ptpv2": True, "aes67": False}}


def test_ptpv1_only_takes_transmit_timestamps_from_a_card_that_can_share(tmp_path, monkeypatch):
    from virgil import timestamping

    for slots, expected in ((timestamping.Slots(4, "probe"), True), (timestamping.Slots(1, "probe"), False),
                            (timestamping.Slots(None, "unknown"), False)):
        a = _ptpv1_virgil(tmp_path / str(slots.count), monkeypatch, mode="always")
        a.ptp_slots = lambda slots=slots: slots
        asyncio.run(a.check_ptpv1(100.0, settle=0))
        assert a.ptpv1_server.hardware_tx is expected


def test_lost_timestamps_move_ptpv1_to_the_kernel_for_good(tmp_path, monkeypatch):
    from virgil import timestamping

    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="always")
    a.timestamping_path = tmp_path / "timestamping.json"
    a.ptp_slots = lambda: timestamping.Slots(4, "probe")
    asyncio.run(a.check_ptpv1(100.0, settle=0))
    assert a.ptpv1_server.hardware_tx
    readings = iter(["tx_hwtstamp_skipped: 0\n", "", "tx_hwtstamp_skipped: 2\n", ""])

    async def command(*args):
        return next(readings)

    a._command = command
    asyncio.run(a.check_shared_timestamps())  # the baseline
    assert a.ptpv1_server.hardware_tx and a.slots_lost is None
    asyncio.run(a.check_shared_timestamps())  # two skipped since
    assert not a.ptpv1_server.hardware_tx
    assert "lost a transmit timestamp while ptp4l and PTPv1 shared it" in a.slots_lost
    assert any("lost a transmit timestamp" in w for w in a.ptp_warnings(100.0))
    saved = timestamping.load_evidence(a.timestamping_path)
    assert len(saved) == 1 and next(iter(saved.values()))["reason"] == "tx_hwtstamp_skipped rose"


def test_ptp4l_timing_out_is_evidence_too(tmp_path, monkeypatch):
    from virgil import timestamping

    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="always")
    a.timestamping_path = tmp_path / "timestamping.json"
    a.ptp_slots = lambda: timestamping.Slots(4, "probe")
    asyncio.run(a.check_ptpv1(100.0, settle=0))

    async def command(*args):
        return "ptp4l[812]: port 1 (enp1s0): timed out while polling for tx timestamp\n" if args[0] == "journalctl" else ""

    a._command = command
    asyncio.run(a.check_shared_timestamps())
    assert not a.ptpv1_server.hardware_tx and "ptp4l timed out" in a.slots_lost


def test_software_only_ptpv1_is_warned_about(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="always")
    asyncio.run(a.check_ptpv1(100.0, settle=0))
    a.ptpv1_server.timestamps = lambda: {"send": "software", "receive": "software"}
    assert any("software timestamps only" in w for w in a.ptp_warnings(100.0))


def test_ptpv1_only_serves_at_once_with_the_cards_own_mac(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="only")
    a.ptp.own_identity = a.ptp.own_announced_at = None  # no ptp4l, so nothing announced
    a.ptp_watch_started = 99.0
    a.ptp_mac = lambda: "8c:a6:82:71:3b:b6"
    assert a.wants_ptpv2() is False
    assert a.ptpv1_wanted(100.0) == (True, "PTPv1 only, instead of PTPv2")  # no 20 s wait, no PTPv2 needed


def test_ptpv1_has_the_slots_to_itself_without_ptp4l(tmp_path, monkeypatch):
    from virgil import timestamping

    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="only")
    a.ptp.own_announced_at = None
    a.ptp_slots = lambda: timestamping.Slots(1, "probe", shareable=False)
    assert a.ptpv1_hardware_tx() is True  # one slot, but ptp4l isn't using it
    a.ptp_slots = lambda: timestamping.Slots(0, "probe", shareable=False)
    assert a.ptpv1_hardware_tx() is False  # no hardware transmit timestamps at all


def test_switching_into_ptpv1_only_makes_before_it_breaks(tmp_path, monkeypatch):
    from virgil import timestamping

    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="fallback")
    a.config.ptpv1 = "only"
    a.ptp_mac = lambda: "8c:a6:82:71:3b:b6"
    a.ptp_slots = lambda: timestamping.Slots(1, "probe", shareable=False)
    order = []

    class Timing:
        request = replace_request = None

        def poke(self):
            order.append(("ptp4l wanted", a.serve_ptpv2_wanted))
            self.request = type("Request", (), {"serve_ptpv2": a.serve_ptpv2_wanted})()

    a.timing = Timing()
    a.ptp.own_announced_at = time.monotonic()  # ptp4l is serving to begin with

    async def scenario():
        await a.switch_ptpv1_mode(True, settle=0, follow_wait=0)
        order.append(("PTPv1 hardware tx", a.ptpv1_server.hardware_tx))

    asyncio.run(scenario())
    assert a.ptpv1_server is not None and a.ptpv1_server.running  # PTPv1 started first...
    assert order == [("ptp4l wanted", False), ("PTPv1 hardware tx", True)]  # ...then ptp4l stopped, then the slot
    assert a.serving()["v1"] is True


def test_switching_back_waits_for_ptp4l_to_announce(tmp_path, monkeypatch):
    a = _ptpv1_virgil(tmp_path, monkeypatch, mode="only")
    a.config.ptpv1 = "fallback"
    a.serve_ptpv2_wanted = False
    a.ptp.own_announced_at = None
    a.ptp_mac = lambda: "8c:a6:82:71:3b:b6"
    announced = []

    class Timing:
        request = None

        def poke(self):
            announced.append(True)  # ptp4l starts, and its first Announce goes out
            a.ptp.own_announced_at = time.monotonic()

    a.timing = Timing()
    asyncio.run(a.switch_ptpv1_mode(False, settle=0))
    assert a.serve_ptpv2_wanted is True and announced == [True] and a.serving()["v2"] is True


def test_the_phc2sys_settings_reach_the_timing_request(tmp_path):
    from virgil.timing import Target

    paths, identities, configs = make_pair(tmp_path)
    config = config_file.load(paths["A"])
    config.ptp_interface, config.phc2sys_kp, config.phc2sys_ki = "lo", 0.015, 0.0002
    config.chrony_filter = 4
    config_file.save(config, paths["A"])
    a = Daemon(paths["A"], identities["A"])
    request = a.build_request(Target(Role.LEADER))
    assert (request.tuning.phc2sys_kp, request.tuning.phc2sys_ki) == (0.015, 0.0002)
    assert request.tuning.filter == 4


def test_leftover_chrony_sample_settings_do_no_harm(tmp_path):
    """Box B had chrony_maxsamples, then chrony_minsamples, set during experiments; neither may trip anything up."""
    path = tmp_path / "virgil.toml"
    config_file.save(config_file.new_config(), path)
    path.write_text(path.read_text() + "chrony_maxsamples = 512\nchrony_minsamples = 128\n")
    assert config_file.load(path).chrony_filter == 8  # the default


def test_a_late_burst_after_a_quiet_night_is_counted_in_full(tmp_path):
    """The rate is since the previous reading: since the count last changed, 189 late packets after hours of none
    were divided by hours, drawn as a spike labelled 0, totalling 0."""
    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])

    class Consoles:
        polls, late = 0, 1996

        def summaries(self):
            return [{"name": "WingCompact", "late_packets": self.late}]

    consoles = Consoles()
    b.consoles = consoles
    t = 1000.0
    for _ in range(3000):  # hours of polls with no new late packets
        consoles.polls += 1
        b.sample_graphs(t)
        t += 5.0
    consoles.late, consoles.polls = 2185, consoles.polls + 1
    b.sample_graphs(t)
    series = b.graphs.window(60, t)
    late = next(s for s in series if s["chart"] == "late")
    assert late["summary"] == {"kind": "total", "value": pytest.approx(189.0)}


def test_a_members_log_is_sent_at_a_limited_rate(tmp_path, monkeypatch):
    """A log crosses the radio link, so it's paced: sent flat out, it could crowd out the audio."""
    import virgil.daemon as module

    paths, identities, configs = make_pair(tmp_path)
    monkeypatch.setattr(module, "LOG_RATE", 200 * 1024)
    monkeypatch.setattr(module, "LOG_CHUNK", 16 * 1024)

    async def scenario():
        a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
        b.recordings_root = tmp_path / "recordings"  # never the real /var/lib/dante-virgil
        runners = await start(a) + await start(b)
        try:
            introduce(a, b)
            log = b.recordings_root / "virgil-20261120-013227-Box-B.tar.gz"
            log.parent.mkdir(parents=True, exist_ok=True)
            content = bytes(range(256)) * 1600  # 400 KB
            log.write_bytes(content)
            started = time.monotonic()
            data = await a._member_call(configs["B"].id, "GET", f"{module.RECORDINGS_PATH}/{log.name}",
                                        timeout=30, as_bytes=True)
            return data == content, time.monotonic() - started
        finally:
            for runner in runners:
                await runner.cleanup()

    intact, took = asyncio.run(scenario())
    assert intact and took >= 1.7  # 400 KB at 200 KB/s: about two seconds, not a blink


def test_pings_across_the_dante_network(tmp_path):
    """Each member's PTP address comes in its heartbeats; pinging it gives separate lines on the link charts."""
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.config.ptp_interface, a.config.mgmt_interface = "enp1s0", "enp5s0"
    a.pings_to = lambda virgil_id: True  # whichever ID sorts first, here A is the one that pings
    beat = Daemon(paths["B"], identities["B"]).heartbeat()
    from dataclasses import replace

    from virgil.daemon import Peer
    a.peers[configs["B"].id] = Peer(replace(beat, ptp_address="192.168.20.246"), time.monotonic())
    assert a.dante_ping_targets() == {"192.168.20.246"}
    assert a.dante_pinger() is not None and a.dante_pinger().dscp == 46
    a.on_dante_ping("192.168.20.246", 1_100_000.0)
    a.on_dante_ping("192.168.20.246", None)
    labels = {s.label for s in a.graphs.series.values()}
    assert "Virgil A ↔ Virgil B (Dante)" in labels
    loss = a.graphs.series["loss/Virgil A/Virgil B/dante"].points
    assert [v for _, v in loss] == [0.0, 100.0]
    a.config.mgmt_interface = "enp1s0"  # one interface for both: the usual pings already cover it
    assert a.dante_ping_targets() == set() and a.dante_pinger() is None


def test_the_ptp_address_travels_in_heartbeats(tmp_path):
    from dataclasses import replace

    from virgil.heartbeat import HeartbeatError, decode

    paths, identities, configs = make_pair(tmp_path)
    beat = replace(Daemon(paths["A"], identities["A"]).heartbeat(), ptp_address="192.168.20.212")
    data = dict(beat.encode())
    assert decode(data).ptp_address == "192.168.20.212"
    data["ptp_address"] = "not an address"
    with pytest.raises(HeartbeatError):
        decode(data)


def test_only_one_of_each_pair_pings(tmp_path):
    """A round trip is the same from either end, so one trace per pair, not two."""
    paths, identities, configs = make_pair(tmp_path)
    a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
    assert a.pings_to(configs["B"].id) != b.pings_to(configs["A"].id)
    pinger, other = (a, b) if a.pings_to(configs["B"].id) else (b, a)
    other_address = "192.168.30.15"
    pinger.addresses_for = other.addresses_for = lambda virgil_id: [other_address]
    assert pinger.ping_targets() == {other_address} and other.ping_targets() == set()


def test_each_graph_series_thins_as_combine_for_says(tmp_path):
    """Replay rebuilds windows from logged readings, which don't say how each series thins: graphs.combine_for does,
    and must agree with how virgild actually records every series."""
    from virgil.graphs import combine_for

    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])

    class Consoles:
        polls = 1

        def summaries(self):
            return [{"name": "WingRack", "late_packets": 5, "latency_ns": 1_200_000, "latency_average_ns": 1_100_000,
                     "latency_setting_ns": 3_000_000, "clock_offset_ppb": -2000, "follows_this_virgil": True}]

    b.consoles = Consoles()
    b.clock = {"system_offset_ns": 100, "phc_offset_ns": 50}
    b.sample_graphs(1000.0)
    b.consoles.polls += 1
    b.sample_graphs(1002.0)
    b.on_ping("192.168.30.101", 1_000_000.0)
    b.on_ping("192.168.30.101", 1_100_000.0)
    assert {k.split("/")[0] for k in b.graphs.series} >= {"latency", "average", "limit", "late", "console", "rtt", "jitter", "loss"}
    for key, series in b.graphs.series.items():
        assert series.combine == combine_for(key), key
