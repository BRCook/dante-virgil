"""Telling people a newer Virgil exists (and never installing anything)."""

import asyncio

import pytest
from aiohttp import web

from tests.test_daemon import make_pair
from tests.test_webapi import HEADERS, _login, run
from tests.test_webapi import virgil  # noqa: F401 (the fixture)
from virgil import updates
from virgil.daemon import Daemon


def test_versions_compare_as_numbers():
    assert updates.parse_version("v1.0.1") == (1, 0, 1) and updates.parse_version("1.10") == (1, 10)
    assert updates.parse_version("v1.0.1-rc1") is None and updates.parse_version(None) is None
    assert updates.newer("1.0.10", "1.0.9") and updates.newer("1.1", "1.0.9") and updates.newer("2", "1.9.9")
    assert not updates.newer("1.0.0", "1.0") and not updates.newer("1.0.1", "1.0.1")
    assert not updates.newer("9.9.9", "unknown")  # a development copy is never called out of date


def test_a_release_is_read_safely():
    found = updates.from_release({"tag_name": "v1.2.0", "html_url": updates.RELEASES + "tag/v1.2.0"}, "1.0.1")
    assert found == {"latest": "1.2.0", "url": updates.RELEASES + "tag/v1.2.0", "available": True}
    # The page shows the link, so only ever to this project's releases.
    elsewhere = updates.from_release({"tag_name": "v1.2.0", "html_url": "https://example.com/evil"}, "1.0.1")
    assert elsewhere["url"] == updates.RELEASES + "latest"
    with pytest.raises(ValueError, match="unexpected tag"):
        updates.from_release({"tag_name": "nightly"}, "1.0.1")


def test_asking_github(monkeypatch):
    seen = {}

    async def latest(request):
        seen["agent"] = request.headers.get("User-Agent")
        return web.json_response({"tag_name": "v1.0.2", "html_url": updates.RELEASES + "tag/v1.0.2"})

    async def broken(request):
        return web.Response(status=403)

    async def scenario():
        app = web.Application()
        app.router.add_get("/latest", latest)
        app.router.add_get("/broken", broken)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            monkeypatch.setattr(updates, "LATEST_URL", f"http://127.0.0.1:{port}/latest")
            found = await updates.fetch_latest("1.0.1")
            monkeypatch.setattr(updates, "LATEST_URL", f"http://127.0.0.1:{port}/broken")
            with pytest.raises(ValueError, match="403"):
                await updates.fetch_latest("1.0.1")
            return found
        finally:
            await runner.cleanup()

    assert asyncio.run(scenario())["available"] and seen["agent"] == "Virgil/1.0.1"


def test_virgild_checks_daily_only_while_on_and_quietly(tmp_path, monkeypatch):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    calls = []

    async def fetch(running):
        calls.append(running)
        if len(calls) == 2:
            raise OSError("no route to host")  # a Clock Box without internet access: normal, not an alert
        return {"latest": "9.0.0", "url": updates.RELEASES + "tag/v9.0.0", "available": True}

    monkeypatch.setattr(updates, "fetch_latest", fetch)

    async def scenario():
        a.config.check_updates = False
        task = asyncio.create_task(a.watch_updates(first=0.05, every=0.05))
        await asyncio.sleep(0.2)
        off = len(calls)  # off: never asked
        a.config.check_updates = True
        a.wake_update_check()  # turned on: check now
        await asyncio.sleep(0.02)
        found = dict(a.status_data()["updates"])
        await asyncio.sleep(0.06)  # the next check fails
        failed = dict(a.status_data()["updates"])
        task.cancel()
        return off, found, failed

    off, found, failed = asyncio.run(scenario())
    assert off == 0
    assert found["available"] and found["latest"] == "9.0.0" and found["enabled"] and "checked" in found
    assert failed["error"] == "no route to host" and failed["latest"] == "9.0.0"  # what it knew, it still says
    assert list(a.events.recent) == []  # neither the news nor the failure is an alert or an event


def test_the_setting_from_the_web_ui(virgil):
    async def scenario(daemon, session, base):
        async with session.post(f"{base}/api/v1/check-updates", json={"enabled": False}, headers=HEADERS) as reply:
            refused = reply.status
        await _login(session, base)
        async with session.post(f"{base}/api/v1/check-updates", json={"enabled": False}, headers=HEADERS):
            pass
        off = daemon.config.check_updates
        daemon._update_wake.clear()
        async with session.post(f"{base}/api/v1/check-updates", json={"enabled": True}, headers=HEADERS):
            pass
        return refused, off, daemon.config.check_updates, daemon._update_wake.is_set()

    refused, off, on, woken = run(virgil, scenario)
    assert refused == 401 and off is False and on is True and woken  # turned on: checks now, not tomorrow


def test_virgilctl_status_says_when_a_newer_virgil_or_another_version_is_about():
    from virgil.cli import _update_lines

    status = {"version": "1.0.1", "updates": {"available": True, "latest": "1.1.0", "running": "1.0.1",
                                             "url": updates.RELEASES + "tag/v1.1.0"},
              "group": {"members": [{"name": "Box B", "version": "1.0.0"}, {"name": "Box C", "version": "1.0.1"}]}}
    assert _update_lines(status) == [f"Virgil 1.1.0 is available (this is 1.0.1): {updates.RELEASES}tag/v1.1.0",
                                     "Box B runs Virgil 1.0.0, and this Virgil 1.0.1"]
    assert _update_lines({"version": "1.0.1", "updates": {"available": False}}) == []
