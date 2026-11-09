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


def test_the_page_and_its_files(virgil):
    async def scenario(daemon, session, base):
        results = {}
        for path in ("/", "/static/app.js", "/static/graphs.js", "/static/style.css", "/static/../daemon.py",
                     "/static/secret"):
            async with session.get(f"{base}{path}") as reply:
                results[path] = (reply.status, reply.headers.get("Content-Security-Policy", ""),
                                 reply.headers.get("X-Content-Type-Options"))
        return results

    results = run(virgil, scenario)
    for path in ("/", "/static/app.js", "/static/graphs.js", "/static/style.css"):
        status, policy, nosniff = results[path]
        assert status == 200 and "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
        assert nosniff == "nosniff"
    assert results["/static/secret"][0] == 404
    assert results["/static/../daemon.py"][0] == 404


def test_status_includes_the_device_toggles(virgil):
    path, identity = virgil
    from virgil.config import DeviceSettings

    config = config_file.load(path)
    config.devices["00:1d:c1:aa:bb:cc"] = DeviceSettings(native_ptpv2=True)
    config_file.save(config, path)

    async def scenario(daemon, session, base):
        async with session.get(f"{base}/api/v1/status") as reply:
            return (await reply.json())["device_settings"]

    assert run(virgil, scenario) == {"00:1d:c1:aa:bb:cc": {"native_ptpv2": True, "aes67": False}}


def test_the_page_never_builds_html_from_strings():
    """Console names come from the network, so the script must never use innerHTML and friends."""
    from pathlib import Path

    import virgil

    for name in ("app.js", "graphs.js"):
        script = (Path(virgil.__file__).parent / "web" / name).read_text()
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert risky not in script, f"{risky} in {name}"


def test_choosing_interfaces(virgil, monkeypatch):
    import virgil.setup as setup_module
    from tests.test_setup import INTERFACES

    import virgil.webapi as webapi_module

    monkeypatch.setattr(setup_module, "get_interface", INTERFACES.get)
    monkeypatch.setattr(setup_module, "list_interfaces", lambda: list(INTERFACES.values()))
    monkeypatch.setattr(webapi_module, "configured", lambda: None)  # not an ifupdown machine

    async def scenario(daemon, session, base):
        restarts = []
        daemon.request_restart = lambda: restarts.append(True)
        await _login(session, base)

        async def choose(**body):
            async with session.post(f"{base}/api/v1/interfaces", json=body, headers=HEADERS) as reply:
                return reply.status, await reply.json()

        async with session.get(f"{base}/api/v1/interfaces") as reply:
            listed = await reply.json()
        results = {
            "listed": listed,
            "profile_asked": await choose(ptp_interface="enp191s0", mgmt_interface="enp5s0"),
            "software_asked": await choose(ptp_interface="enp5s0", mgmt_interface="enp5s0"),
            "bad": await choose(ptp_interface="br0", mgmt_interface="enp5s0"),
            "accepted": await choose(ptp_interface="enp191s0", mgmt_interface="enp5s0", accept_profile=True),
        }
        results["unchanged"] = await choose(ptp_interface="enp191s0", mgmt_interface="enp5s0", accept_profile=True)
        return results, daemon.config, restarts

    results, config, restarts = run(virgil, scenario)
    assert {i["name"] for i in results["listed"]["interfaces"]} == set(INTERFACES)
    assert results["profile_asked"][0] == 409 and results["profile_asked"][1]["needs"] == "profile"
    assert results["software_asked"][1]["needs"] == "software"
    assert results["bad"][0] == 400
    assert results["accepted"][0] == 200 and results["accepted"][1]["restarting"] is True
    assert (config.ptp_interface, config.nic_profile, config.mgmt_interface) == ("enp191s0", "r8126", "enp5s0")
    assert results["unchanged"][1]["message"] == "Nothing changed"
    assert restarts == [True]


def test_choosing_interfaces_needs_a_login(virgil):
    async def scenario(daemon, session, base):
        async with session.post(f"{base}/api/v1/interfaces", json={"ptp_interface": "x", "mgmt_interface": "y"},
                                headers=HEADERS) as reply:
            return reply.status

    assert run(virgil, scenario) == 401


def test_choosing_an_interface_ifupdown_wont_bring_up(virgil, monkeypatch):
    import virgil.setup as setup_module
    import virgil.webapi as webapi_module
    from tests.test_setup import INTERFACES

    monkeypatch.setattr(setup_module, "get_interface", INTERFACES.get)
    monkeypatch.setattr(webapi_module, "configured", lambda: {"lo", "enp5s0"})  # Box B: no enp1s0 stanza

    async def scenario(daemon, session, base):
        added, restarts = [], []
        daemon.update_dhcp_names = lambda add=None, force=False: added.append(add)
        daemon.request_restart = lambda: restarts.append(True)
        await _login(session, base)

        async def choose(**body):
            async with session.post(f"{base}/api/v1/interfaces", json=body, headers=HEADERS) as reply:
                return reply.status, await reply.json()

        asked = await choose(ptp_interface="enp1s0", mgmt_interface="enp5s0")
        done = await choose(ptp_interface="enp1s0", mgmt_interface="enp5s0", add_stanzas=True)
        return asked, done, added, restarts

    asked, done, added, restarts = run(virgil, scenario)
    assert asked[0] == 409 and asked[1]["needs"] == "stanza" and asked[1]["interfaces"] == ["enp1s0"]
    assert "isn't set up to come up" in asked[1]["message"]
    assert done[0] == 200 and added == [["enp1s0"]] and restarts == [True]


def test_recording_from_the_page(virgil, tmp_path):
    async def scenario(daemon, session, base):
        daemon.recordings_root = tmp_path / "recordings"
        daemon.recording.root = daemon.recordings_root
        daemon.recording.units = None
        async with session.get(f"{base}/api/v1/recordings") as reply:
            anonymous_list = reply.status
        await _login(session, base)

        async def post(path, body=None):
            async with session.post(f"{base}{path}", json=body or {}, headers=HEADERS) as reply:
                return reply.status, await reply.json()

        started = await post("/api/v1/recording/start", {"group": False})
        again = await post("/api/v1/recording/start")
        noted = await post("/api/v1/recording/note", {"text": "baseline start"})
        async with session.get(f"{base}/api/v1/status") as reply:
            recording_state = (await reply.json())["recording"]
        await asyncio.sleep(0.1)
        stopped = await post("/api/v1/recording/stop")
        async with session.get(f"{base}{stopped[1]['download']}") as reply:
            download = reply.status, reply.headers.get("Content-Disposition", ""), len(await reply.read())
        async with session.get(f"{base}/api/v1/recordings") as reply:
            listed = (await reply.json())["recordings"]
        deleted = await post("/api/v1/recordings/delete", {"all": True})
        return anonymous_list, started, again, noted, recording_state, stopped, download, listed, deleted

    anonymous, started, again, noted, state, stopped, download, listed, deleted = run(virgil, scenario)
    assert anonymous == 401
    assert started[0] == 200 and again[0] == 409 and noted[0] == 200
    assert state["active"] is True
    assert download[0] == 200 and "attachment" in download[1] and download[2] > 0
    assert [r["name"] for r in listed] == [stopped[1]["archive"]]
    assert deleted == (200, {"deleted": 1})
