"""virgild: Virgil's daemon.

So far it serves the public port (status, and joining a group) and the group
port (members only, by certificate). Heartbeats, the election and discovery
come next.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from virgil import __version__
from virgil import config as config_file
from virgil import tls
from virgil.config import Config
from virgil.group import GROUP_PORT, PUBLIC_PORT
from virgil.joining import JOIN_PATH, JoinError, handle_join
from virgil.tls import Identity, fingerprint

log = logging.getLogger("virgild")

CONFIG_CHECK_INTERVAL = 2.0


@dataclass
class Daemon:
    config_path: Path
    identity: Identity
    config: Config = field(init=False)
    group_context: ssl.SSLContext = field(init=False)
    trusted: set[str] = field(default_factory=set, init=False)
    config_mtime: float = field(default=0.0, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.config = config_file.load(self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime
        self.group_context = tls.group_server_context(self.identity, [])
        self._trust_members()

    def member_certs(self) -> list[str]:
        if self.config.group is None:
            return []
        return [m.cert for m in self.config.group.members.values()]

    def _trust_members(self) -> None:
        """Add any new members' certificates to the group port's trust store.

        Certificates can be added to a running TLS context but not removed, so
        a removed member stays trusted until virgild restarts.
        """
        for cert in self.member_certs():
            fp = fingerprint(cert)
            if fp not in self.trusted:
                self.group_context.load_verify_locations(cadata=cert)
                self.trusted.add(fp)
        current = {fingerprint(c) for c in self.member_certs()}
        if self.trusted - current:
            log.warning("A member was removed; it stays trusted until virgild restarts")

    def reload_if_changed(self) -> None:
        """Pick up changes virgilctl made to the config file."""
        mtime = self.config_path.stat().st_mtime
        if mtime == self.config_mtime:
            return
        self.config = config_file.load(self.config_path)
        self.config_mtime = mtime
        self._trust_members()
        log.info("Reloaded the config")

    def save(self) -> None:
        config_file.save(self.config, self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime

    # Public port

    async def status(self, request: web.Request) -> web.Response:
        group = self.config.group
        return web.json_response(
            {
                "product": "Virgil",
                "version": __version__,
                "id": self.config.id,
                "name": self.config.name,
                "group": None if group is None else {"id": group.id, "members": len(group.members)},
            }
        )

    async def join(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "the request must be JSON"}, status=400)
        async with self.lock:
            # Invites are made by virgilctl, so read the latest config first.
            self.reload_if_changed()
            # Work on a copy, so a refused request changes nothing.
            updated = copy.deepcopy(self.config)
            try:
                reply = handle_join(updated, body, time.time())
            except JoinError as error:
                log.warning("Refused a join request from %s: %s", request.remote, error)
                return web.json_response({"error": str(error)}, status=error.status)
            self.config = updated
            self.save()
            self._trust_members()
        log.info("%s joined the group from %s", body.get("name"), request.remote)
        return web.json_response(reply)

    def public_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/api/v1/status", self.status)
        app.router.add_post(JOIN_PATH, self.join)
        return app

    # Group port

    async def ping(self, request: web.Request) -> web.Response:
        return web.json_response({"id": self.config.id, "name": self.config.name})

    def group_app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/group/v1/ping", self.ping)
        return app

    async def watch_config(self) -> None:
        while True:
            await asyncio.sleep(CONFIG_CHECK_INTERVAL)
            try:
                self.reload_if_changed()
            except Exception:
                log.exception("Couldn't reload the config")


async def serve(daemon: Daemon, host: str, public_port: int, group_port: int) -> None:
    public = web.AppRunner(daemon.public_app(), access_log=None)
    group = web.AppRunner(daemon.group_app(), access_log=None)
    await public.setup()
    await group.setup()
    await web.TCPSite(public, host, public_port, ssl_context=tls.public_server_context(daemon.identity)).start()
    await web.TCPSite(group, host, group_port, ssl_context=daemon.group_context).start()
    log.info("Virgil %s: %s (%s), public port %d, group port %d",
             __version__, daemon.config.name, daemon.config.id, public_port, group_port)
    try:
        await daemon.watch_config()
    finally:
        await public.cleanup()
        await group.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="virgild", description="Virgil's daemon.")
    parser.add_argument("--config", type=Path, default=config_file.DEFAULT_PATH)
    parser.add_argument("--tls-dir", type=Path, default=tls.TLS_DIR)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--public-port", type=int, default=PUBLIC_PORT)
    parser.add_argument("--group-port", type=int, default=GROUP_PORT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    daemon = Daemon(args.config, tls.default_identity(args.tls_dir))
    asyncio.run(serve(daemon, args.host, args.public_port, args.group_port))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
