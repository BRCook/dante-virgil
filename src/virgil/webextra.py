"""Two small servers alongside the web UI.

- A plain-HTTP server on port 80 that redirects to HTTPS, so typing a Virgil's
  address into a browser works.
- netaudio's web UI on its own HTTPS port, passed through to netaudio's daemon
  on loopback. netaudio's daemon treats loopback callers as its owner, with
  full control over the consoles, so this is behind Virgil's login: the
  session cookie a browser gets from Virgil's page is sent to every port on
  the same host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import aiohttp
from aiohttp import web

from virgil.webauth import COOKIE

log = logging.getLogger("virgild.web")

HTTP_PORT = 80
HTTPS_PORT = 443
NETAUDIO_PROXY_PORT = 8742
NETAUDIO_URL = "http://127.0.0.1:4780"

# Headers that belong to one connection, not the request or response itself.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length",
}


def redirect_app() -> web.Application:
    async def redirect(request: web.Request) -> web.Response:
        host = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
        raise web.HTTPMovedPermanently(f"https://{host}{request.path_qs}")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", redirect)
    return app


def _login_page(request: web.Request) -> web.Response:
    host = request.host.rsplit(":", 1)[0]
    page = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Log in to Virgil</title><body style='font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;"
        "padding:0 1rem'><h1>Log in to Virgil first</h1><p>netaudio can change the consoles' settings, so it's "
        "behind Virgil's password.</p>"
        f"<p><a href='https://{host}/'>Open Virgil and log in</a>, then come back here.</p></body>"
    )
    return web.Response(text=page, content_type="text/html", status=401)


def netaudio_app(logged_in: Callable[[str | None], bool], upstream: str = NETAUDIO_URL) -> web.Application:
    """netaudio's web UI and API, for logged-in Virgil users only."""
    session: aiohttp.ClientSession | None = None

    async def start(app: web.Application) -> None:
        nonlocal session
        # Pass responses through untouched, gzip and all, and never time out a live event stream.
        session = aiohttp.ClientSession(auto_decompress=False,
                                        timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=None))

    async def stop(app: web.Application) -> None:
        if session is not None:
            await session.close()

    async def proxy(request: web.Request) -> web.StreamResponse:
        if not logged_in(request.cookies.get(COOKIE)):
            return _login_page(request)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "cookie"}
        headers["X-Forwarded-Proto"] = "https"
        body = await request.read() if request.can_read_body else None
        try:
            reply_context = session.request(request.method, upstream + request.path_qs, headers=headers, data=body,
                                            allow_redirects=False)
            reply = await reply_context.__aenter__()
        except (aiohttp.ClientConnectionError, TimeoutError) as error:
            log.warning("Couldn't reach netaudio's daemon for %s: %s", request.path, error)
            return web.Response(text="netaudio's daemon isn't running on this Virgil.", status=502)
        try:
            response = web.StreamResponse(status=reply.status, reason=reply.reason)
            for key, value in reply.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    response.headers.add(key, value)
            await response.prepare(request)
            try:
                async for chunk in reply.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
            except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                pass  # The browser went away, such as by closing a live event stream. Nothing to report.
            except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError) as error:
                log.info("netaudio's daemon stopped mid-reply for %s: %s", request.path, error)
            return response
        finally:
            await reply_context.__aexit__(None, None, None)

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app
