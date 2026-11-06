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
from virgil.group import Member, create
from virgil.heartbeat import HEARTBEAT_PATH, Heartbeat

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
    """Tell one daemon where the other is, as mDNS would."""
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
