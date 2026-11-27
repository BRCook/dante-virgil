"""The rest of item 8 of the 2.0 plan: escaping, a file handle, and headers on error replies."""

import asyncio
import ssl

import aiohttp
from aiohttp import web

from tests.test_daemon import make_pair, start
from virgil import summary
from virgil.daemon import CONTENT_SECURITY_POLICY, Daemon, security_headers


def test_error_replies_carry_the_security_headers(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def boom(request):
        raise RuntimeError("something unexpected")

    async def scenario():
        a = Daemon(paths["A"], identities["A"])
        runners = await start(a)
        app = web.Application(middlewares=[security_headers])
        app.router.add_get("/boom", boom)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://127.0.0.1:{a.public_port}/no-such-page", ssl=context) as reply:
                    missing = (reply.status, reply.headers.get("Content-Security-Policy"))
                async with session.get(f"http://127.0.0.1:{port}/boom") as reply:
                    failed = (reply.status, reply.headers.get("Content-Security-Policy"), await reply.text())
            return missing, failed
        finally:
            await runner.cleanup()
            for r in runners:
                await r.cleanup()

    missing, failed = asyncio.run(scenario())
    assert missing == (404, CONTENT_SECURITY_POLICY)
    assert failed[:2] == (500, CONTENT_SECURITY_POLICY) and "something unexpected" not in failed[2]


def test_a_logs_state_names_are_escaped_in_the_summary():
    hostile = {"virgil": "Box A", "from": 0.0, "to": 60.0, "status_readings": 2,
               "states": {"<script>alert(1)</script>": 2}, "measurements": [], "devices": [], "alerts": [],
               "events": [], "notes": []}
    page = summary.render([hostile])
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_the_journals_file_is_closed_once_journalctl_has_it():
    import inspect

    from virgil import recording

    source = inspect.getsource(recording.Session.start)
    assert 'with (folder / "journal.log").open("w") as journal:' in source
