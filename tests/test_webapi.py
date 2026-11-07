"""The web API, through a real daemon's public port."""

import asyncio
import shutil

import aiohttp
import pytest
from aiohttp import web

from virgil import config as config_file
from virgil import tls
from virgil.daemon import Daemon
from virgil.election import State
from virgil.webauth import hash_password

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

PASSWORD = "showtime-2026"
HEADERS = {"X-Virgil-Request": "1"}


@pytest.fixture
def virgil(tmp_path):
    identity = tls.default_identity(tmp_path / "tls")
    tls.generate(identity, hostname="a")
    config = config_file.new_config()
    config.name = "Box A"
    config.web_password = hash_password(PASSWORD)
    path = tmp_path / "virgil.toml"
    config_file.save(config, path)
    return path, identity


async def _serve(daemon):
    runner = web.AppRunner(daemon.public_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls.public_server_context(daemon.identity))
    await site.start()
    return runner, f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _session():
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                 connector=aiohttp.TCPConnector(ssl=False))


def run(virgil, scenario):
    path, identity = virgil

    async def main():
        daemon = Daemon(path, identity)
        runner, base = await _serve(daemon)
        try:
            async with _session() as session:
                return await scenario(daemon, session, base)
        finally:
            await runner.cleanup()

    return asyncio.run(main())


async def _login(session, base, password=PASSWORD):
    async with session.post(f"{base}/api/v1/login", json={"password": password}, headers=HEADERS) as reply:
        return reply.status


def test_status_needs_no_login(virgil):
    async def scenario(daemon, session, base):
        async with session.get(f"{base}/api/v1/status") as reply:
            return reply.status, (await reply.json())["name"]

    assert run(virgil, scenario) == (200, "Box A")


def test_changes_need_a_login(virgil):
    async def scenario(daemon, session, base):
        async with session.post(f"{base}/api/v1/trim", json={"ppm": -5}, headers=HEADERS) as reply:
            before = reply.status
        assert await _login(session, base) == 200
        async with session.post(f"{base}/api/v1/trim", json={"ppm": -5.146}, headers=HEADERS) as reply:
            after = reply.status
        return before, after, daemon.config.trim_ppb

    assert run(virgil, scenario) == (401, 200, -5146)


def test_wrong_passwords_and_guessing(virgil):
    async def scenario(daemon, session, base):
        statuses = [await _login(session, base, "nope") for _ in range(6)]
        return statuses, await _login(session, base)  # right password, but blocked for now

    statuses, last = run(virgil, scenario)
    assert statuses == [401] * 5 + [429] and last == 429


def test_other_sites_are_refused(virgil):
    async def scenario(daemon, session, base):
        await _login(session, base)
        async with session.post(f"{base}/api/v1/trim", json={"ppm": 1}) as reply:  # no header
            no_header = reply.status
        headers = {**HEADERS, "Origin": "https://evil.example"}
        async with session.post(f"{base}/api/v1/trim", json={"ppm": 1}, headers=headers) as reply:
            other_origin = reply.status
        return no_header, other_origin, daemon.config.trim_ppb

    assert run(virgil, scenario) == (403, 403, 0)


def test_cookie_is_locked_down(virgil):
    async def scenario(daemon, session, base):
        async with session.post(f"{base}/api/v1/login", json={"password": PASSWORD}, headers=HEADERS) as reply:
            return reply.headers.get("Set-Cookie", "")

    cookie = run(virgil, scenario)
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_logout(virgil):
    async def scenario(daemon, session, base):
        await _login(session, base)
        async with session.post(f"{base}/api/v1/logout") as reply:
            pass
        async with session.get(f"{base}/api/v1/session") as reply:
            return await reply.json()

    assert run(virgil, scenario) == {"logged_in": False, "password_set": True}


def test_no_password_means_read_only(virgil):
    path, identity = virgil
    config = config_file.load(path)
    config.web_password = None
    config_file.save(config, path)

    async def scenario(daemon, session, base):
        return await _login(session, base, "anything")

    assert run(virgil, scenario) == 409


def test_actions(virgil):
    async def scenario(daemon, session, base):
        await _login(session, base)

        async def post(path, body=None):
            async with session.post(f"{base}{path}", json=body or {}, headers=HEADERS) as reply:
                return reply.status, await reply.json()

        results = {
            "auto_trim": await post("/api/v1/auto-trim", {"enabled": False}),
            "device": await post("/api/v1/device", {"mac": "001DC1AABBCC", "setting": "ptpv2", "enabled": True}),
            "bad_device": await post("/api/v1/device", {"mac": "nope", "setting": "ptpv2", "enabled": True}),
            "bad_trim": await post("/api/v1/trim", {"ppm": "lots"}),
            "lead": await post("/api/v1/lead"),  # observing only in this test
            "invite_alone": await post("/api/v1/group/invite"),
            "create": await post("/api/v1/group/create"),
            "create_again": await post("/api/v1/group/create"),
        }
        daemon.addresses = ("127.0.0.1",)
        results["invite"] = await post("/api/v1/group/invite", {"minutes": 5})
        return results, daemon.config

    results, config = run(virgil, scenario)
    assert results["auto_trim"][0] == 200 and config.auto_trim is False
    assert results["device"][0] == 200 and config.devices["00:1d:c1:aa:bb:cc"].native_ptpv2
    assert results["bad_device"][0] == 400 and results["bad_trim"][0] == 400
    assert results["lead"][0] == 409
    assert results["invite_alone"][0] == 409
    assert results["create"][0] == 200 and results["create_again"][0] == 409
    assert results["invite"][0] == 200 and results["invite"][1]["token"].startswith("virgil1.")


def test_join_through_the_web(tmp_path, virgil):
    """Box B joins Box A's group by pasting A's invite into B's web UI."""
    path_a, identity_a = virgil
    identity_b = tls.default_identity(tmp_path / "b" / "tls")
    tls.generate(identity_b, hostname="b")
    config_b = config_file.new_config()
    config_b.name = "Box B"
    config_b.web_password = hash_password(PASSWORD)
    path_b = tmp_path / "b" / "virgil.toml"
    config_file.save(config_b, path_b)

    async def main():
        a, b = Daemon(path_a, identity_a), Daemon(path_b, identity_b)
        runner_a, base_a = await _serve(a)
        runner_b, base_b = await _serve(b)
        a.public_port = int(base_a.rsplit(":", 1)[1])
        a.addresses = ("127.0.0.1",)
        try:
            async with _session() as session_a, _session() as session_b:
                await _login(session_a, base_a)
                await session_a.post(f"{base_a}/api/v1/group/create", headers=HEADERS)
                async with session_a.post(f"{base_a}/api/v1/group/invite", json={}, headers=HEADERS) as reply:
                    token = (await reply.json())["token"]
                await _login(session_b, base_b)
                async with session_b.post(f"{base_b}/api/v1/group/join", json={"token": token},
                                          headers=HEADERS) as reply:
                    return reply.status, await reply.json(), a.config, b.config
        finally:
            await runner_a.cleanup()
            await runner_b.cleanup()

    status, body, config_a, config_b = asyncio.run(main())
    assert status == 200 and body["members"] == ["Box A"]
    assert config_a.group.id == config_b.group.id
    assert set(config_a.group.members) == set(config_b.group.members)
    assert config_file.load(path_b).group.id == config_a.group.id


def test_stop_and_start(virgil):
    async def scenario(daemon, session, base):
        await _login(session, base)

        class Timing:
            wanted = []

            def want(self, target):
                Timing.wanted.append(target)

        daemon.timing = Timing()
        async with session.post(f"{base}/api/v1/stop", headers=HEADERS) as reply:
            stopped = reply.status, daemon.election.state
        async with session.post(f"{base}/api/v1/start", headers=HEADERS) as reply:
            started = reply.status, daemon.election.state
        return stopped, started, Timing.wanted

    stopped, started, wanted = run(virgil, scenario)
    assert stopped == (200, State.STOPPED) and wanted == [None]
    assert started == (200, State.LISTEN)
