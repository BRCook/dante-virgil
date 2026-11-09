import asyncio
import gzip

import aiohttp
from aiohttp import web

from virgil.webextra import netaudio_app, redirect_app


async def _serve(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def test_http_redirects_to_https():
    async def scenario():
        runner, base = await _serve(redirect_app())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/some/path?x=1", allow_redirects=False,
                                       headers={"Host": "10.10.100.253"}) as reply:
                    plain = reply.status, reply.headers["Location"]
                async with session.get(f"{base}/", allow_redirects=False,
                                       headers={"Host": "10.10.100.253:80"}) as reply:
                    with_port = reply.headers["Location"]
            return plain, with_port
        finally:
            await runner.cleanup()

    (status, location), with_port = asyncio.run(scenario())
    assert status == 301 and location == "https://10.10.100.253/some/path?x=1"
    assert with_port == "https://10.10.100.253/"


class FakeNetaudio:
    def __init__(self):
        self.seen = []

    def app(self):
        app = web.Application()
        app.router.add_get("/", self.page)
        app.router.add_get("/_assets/app.js", self.gzipped)
        app.router.add_get("/events", self.events)
        app.router.add_post("/set-aes67", self.post)
        return app

    async def page(self, request):
        self.seen.append(("GET", request.path_qs, dict(request.headers)))
        return web.Response(text="<html>netaudio</html>", content_type="text/html")

    async def gzipped(self, request):
        return web.Response(body=gzip.compress(b"console.log(1)"), headers={"Content-Encoding": "gzip"},
                            content_type="application/javascript")

    async def events(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for n in range(3):
            await response.write(f"data: {n}\n\n".encode())
            await asyncio.sleep(0.05)
        return response

    async def post(self, request):
        self.seen.append(("POST", request.path, await request.json()))
        return web.json_response({"ok": True})


def test_netaudio_needs_virgils_login_and_passes_everything_through():
    fake = FakeNetaudio()

    async def scenario():
        upstream_runner, upstream = await _serve(fake.app())
        proxy_runner, proxy = await _serve(netaudio_app(lambda token: token == "good", upstream=upstream))
        try:
            results = {}
            async with aiohttp.ClientSession(auto_decompress=False) as session:
                async with session.get(f"{proxy}/") as reply:
                    results["anonymous"] = reply.status, await reply.text()
                cookies = {"virgil_session": "good"}
                async with session.get(f"{proxy}/?view=devices", cookies=cookies) as reply:
                    results["page"] = reply.status, await reply.text()
                async with session.get(f"{proxy}/_assets/app.js", cookies=cookies) as reply:
                    results["asset"] = reply.headers.get("Content-Encoding"), await reply.read()
                async with session.get(f"{proxy}/events", cookies=cookies) as reply:
                    results["events"] = await reply.text()
                async with session.post(f"{proxy}/set-aes67", json={"device": "Wing", "enabled": True},
                                        cookies=cookies) as reply:
                    results["post"] = reply.status
            return results
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    results = asyncio.run(scenario())
    status, text = results["anonymous"]
    assert status == 401 and "Log in to Virgil first" in text
    assert results["page"] == (200, "<html>netaudio</html>")
    method, path, headers = fake.seen[0]
    assert path == "/?view=devices" and "Cookie" not in headers  # Virgil's cookie stays with Virgil
    encoding, body = results["asset"]
    assert encoding == "gzip" and gzip.decompress(body) == b"console.log(1)"
    assert results["events"] == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert results["post"] == 200 and fake.seen[-1] == ("POST", "/set-aes67", {"device": "Wing", "enabled": True})


def test_netaudio_not_running():
    async def scenario():
        runner, proxy = await _serve(netaudio_app(lambda token: True, upstream="http://127.0.0.1:9"))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{proxy}/") as reply:
                    return reply.status, await reply.text()
        finally:
            await runner.cleanup()

    status, text = asyncio.run(scenario())
    assert status == 502 and "isn't running" in text


def test_a_browser_closing_a_live_stream_is_not_an_error(caplog):
    """Closing netaudio's event stream mid-way, as its page does, mustn't be logged as netaudio being unreachable."""
    import logging

    fake = FakeNetaudio()

    async def scenario():
        upstream_runner, upstream = await _serve(fake.app())
        proxy_runner, proxy = await _serve(netaudio_app(lambda token: True, upstream=upstream))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{proxy}/events") as reply:
                    await reply.content.readany()  # read a little, then hang up
            await asyncio.sleep(0.3)
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    caplog.set_level(logging.INFO)
    asyncio.run(scenario())
    assert "Couldn't reach netaudio" not in caplog.text
