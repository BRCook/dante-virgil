import asyncio
import os
import shutil
import ssl
import time

import aiohttp
import pytest
from aiohttp import web

from virgil import config as config_file
from virgil import group as groups
from virgil import tls
from virgil.daemon import Daemon
from virgil.group import GroupError, InviteToken
from virgil.joining import JoinError, handle_join, join

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def make_box(root, name):
    directory = root / name
    identity = tls.default_identity(directory / "tls")
    tls.generate(identity, hostname=name)
    config = config_file.new_config()
    config.name = name
    path = directory / "virgil.toml"
    config_file.save(config, path)
    return path, identity


@pytest.fixture
def boxes(tmp_path):
    return {name: make_box(tmp_path, name) for name in ("a", "b", "c")}


def create_group(path, identity):
    config = config_file.load(path)
    config.group = groups.create(config.id, config.name, identity.cert_pem)
    config_file.save(config, path)


def invite(path, identity, port, lifetime=600):
    config = config_file.load(path)
    secret, expires = groups.create_invite(config.group, time.time(), lifetime)
    config_file.save(config, path)
    return InviteToken(config.group.id, ("127.0.0.1",), port, tls.fingerprint(identity.cert_pem), secret, expires)


async def start(daemon):
    """Start a daemon's two ports on free ports, and return them with a cleanup function."""
    runners = []
    ports = []
    for app, context in ((daemon.public_app(), tls.public_server_context(daemon.identity)),
                         (daemon.group_app(), daemon.group_context)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        runners.append(runner)
        ports.append(site._server.sockets[0].getsockname()[1])

    async def stop():
        for runner in runners:
            await runner.cleanup()

    return ports[0], ports[1], stop


def test_join_through_a_running_daemon(boxes):
    path_a, id_a = boxes["a"]
    path_b, id_b = boxes["b"]
    create_group(path_a, id_a)

    async def scenario():
        daemon = Daemon(path_a, id_a)
        public, group_port, stop = await start(daemon)
        try:
            token = invite(path_a, id_a, public)
            config_b = config_file.load(path_b)
            await join(token, config_b, id_b.cert_pem)
            config_file.save(config_b, path_b)

            # A used invite can't be used again.
            config_c = config_file.load(boxes["c"][0])
            with pytest.raises(GroupError, match="refused"):
                await join(token, config_c, boxes["c"][1].cert_pem)

            # B is now trusted on A's group port, and C isn't.
            group_b = tls.group_client_context(id_b, [id_a.cert_pem])
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://127.0.0.1:{group_port}/group/v1/ping", ssl=group_b) as reply:
                    assert (await reply.json())["name"] == "a"
            group_c = tls.group_client_context(boxes["c"][1], [id_a.cert_pem])
            async with aiohttp.ClientSession() as session:
                with pytest.raises((aiohttp.ClientError, ssl.SSLError)):
                    async with session.get(f"https://127.0.0.1:{group_port}/group/v1/ping", ssl=group_c) as reply:
                        await reply.read()
            return daemon
        finally:
            await stop()

    daemon = asyncio.run(scenario())
    config_a, config_b = config_file.load(path_a), config_file.load(path_b)
    assert config_a.group.id == config_b.group.id
    assert set(config_a.group.members) == set(config_b.group.members) == {config_a.id, config_b.id}
    assert config_a.group.invites == {}
    assert daemon.config.group.members.keys() == config_a.group.members.keys()


def test_join_refuses_the_wrong_box(boxes):
    """If something else answers at the inviting box's address, the join stops before sending anything."""
    path_a, id_a = boxes["a"]
    path_c, id_c = boxes["c"]
    create_group(path_a, id_a)
    create_group(path_c, id_c)

    async def scenario():
        impostor = Daemon(path_c, id_c)
        public, _, stop = await start(impostor)
        try:
            token = invite(path_a, id_a, public)  # A's fingerprint, but C answers
            config_b = config_file.load(boxes["b"][0])
            with pytest.raises(GroupError, match="isn't the one that made the invite"):
                await join(token, config_b, boxes["b"][1].cert_pem)
        finally:
            await stop()

    asyncio.run(scenario())
    assert config_file.load(path_c).group.members.keys() == {config_file.load(path_c).id}


def test_join_with_an_expired_token(boxes):
    path_a, id_a = boxes["a"]
    create_group(path_a, id_a)
    token = invite(path_a, id_a, 1, lifetime=-1)
    with pytest.raises(GroupError, match="expired"):
        asyncio.run(join(token, config_file.load(boxes["b"][0]), boxes["b"][1].cert_pem))


def test_unreachable_inviter(boxes):
    path_a, id_a = boxes["a"]
    create_group(path_a, id_a)
    token = invite(path_a, id_a, 9)  # nothing listens on port 9
    with pytest.raises(GroupError, match="couldn't reach"):
        asyncio.run(join(token, config_file.load(boxes["b"][0]), boxes["b"][1].cert_pem, timeout=2))


def _request(config_b, id_b, secret):
    return {"secret": secret, "id": config_b.id, "name": config_b.name, "cert": id_b.cert_pem}


def test_handle_join_checks(boxes):
    path_a, id_a = boxes["a"]
    create_group(path_a, id_a)
    config_a = config_file.load(path_a)
    config_b = config_file.load(boxes["b"][0])
    id_b = boxes["b"][1]
    secret, _ = groups.create_invite(config_a.group, time.time())

    for body, status in [
        ("nope", 400),
        ({**_request(config_b, id_b, secret), "id": "../x"}, 400),
        ({**_request(config_b, id_b, secret), "name": ""}, 400),
        ({**_request(config_b, id_b, secret), "cert": "junk"}, 400),
        (_request(config_b, id_b, "wrong-secret-but-long-enough"), 403),
    ]:
        with pytest.raises(JoinError) as error:
            handle_join(config_a, body, time.time())
        assert error.value.status == status

    # A different box can't take over an existing member's ID.
    impostor = {**_request(config_b, boxes["c"][1], secret), "id": config_a.id}
    with pytest.raises(JoinError) as error:
        handle_join(config_a, impostor, time.time())
    assert error.value.status == 409


def test_join_when_already_in_a_group(boxes):
    path_a, id_a = boxes["a"]
    create_group(path_a, id_a)
    token = invite(path_a, id_a, 1)
    config = config_file.load(path_a)
    with pytest.raises(GroupError, match="already in a group"):
        asyncio.run(join(token, config, id_a.cert_pem))


def test_daemon_picks_up_config_changes(boxes):
    path_a, id_a = boxes["a"]
    daemon = Daemon(path_a, id_a)
    assert daemon.config.group is None
    time.sleep(0.01)
    create_group(path_a, id_a)
    os.utime(path_a, None)
    daemon.reload_if_changed()
    assert daemon.config.group is not None
    assert tls.fingerprint(id_a.cert_pem) in daemon.trusted
