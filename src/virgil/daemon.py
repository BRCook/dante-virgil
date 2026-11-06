"""virgild: Virgil's daemon.

It serves the public port (status, and joining a group) and the group port
(members only, by certificate), finds other Virgils over mDNS, exchanges
heartbeats with the members of its group, and runs the leader election.

For now the election runs in observe mode: virgild logs every decision it
would make, but doesn't change any timing service. `virgilctl role` still
sets roles by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from virgil import __version__
from virgil import config as config_file
from virgil import discovery, tls
from virgil.config import Config
from virgil.election import Change, Election, State
from virgil.group import GROUP_PORT, PUBLIC_PORT
from virgil.heartbeat import HEARTBEAT_PATH, Heartbeat, HeartbeatError, decode
from virgil.interfaces import ipv4_addresses
from virgil.joining import JOIN_PATH, JoinError, handle_join
from virgil.tls import Identity, fingerprint, fingerprint_of_der

log = logging.getLogger("virgild")

CONFIG_CHECK_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 0.8
ELECTION_INTERVAL = 0.5


@dataclass
class Peer:
    heartbeat: Heartbeat
    heard_at: float  # monotonic
    remote: str | None = None


@dataclass
class Daemon:
    config_path: Path
    identity: Identity
    group_port: int = GROUP_PORT
    public_port: int = PUBLIC_PORT
    observing: bool = True
    config: Config = field(init=False)
    group_context: ssl.SSLContext = field(init=False)
    trusted: set[str] = field(default_factory=set, init=False)
    config_mtime: float = field(default=0.0, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    election: Election = field(init=False)
    peers: dict[str, Peer] = field(default_factory=dict, init=False)
    adverts: dict[str, discovery.Advert] = field(default_factory=dict, init=False)
    # Addresses to try for each member, by Virgil ID, for members without an mDNS advert.
    learned: dict[str, str] = field(default_factory=dict, init=False)
    discovery: discovery.Discovery | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.config = config_file.load(self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime
        self.group_context = tls.group_server_context(self.identity, [])
        self._trust_members()
        self.election = Election(self.config.id, time.monotonic())

    # Config and membership

    def member_certs(self) -> list[str]:
        if self.config.group is None:
            return []
        return [m.cert for m in self.config.group.members.values()]

    def member_ids(self) -> list[str]:
        if self.config.group is None:
            return []
        return [i for i in self.config.group.members if i != self.config.id]

    def name_of(self, virgil_id: str | None) -> str | None:
        if virgil_id is None:
            return None
        if self.config.group is not None and virgil_id in self.config.group.members:
            return self.config.group.members[virgil_id].name
        return virgil_id[:8]

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

    def reload_if_changed(self) -> bool:
        """Pick up changes virgilctl made to the config file."""
        mtime = self.config_path.stat().st_mtime
        if mtime == self.config_mtime:
            return False
        self.config = config_file.load(self.config_path)
        self.config_mtime = mtime
        self._trust_members()
        log.info("Reloaded the config")
        return True

    def save(self) -> None:
        config_file.save(self.config, self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime

    # Heartbeats

    def heartbeat(self) -> Heartbeat:
        e = self.election
        return Heartbeat(self.config.id, self.config.name, __version__, e.state, e.coin, e.leader,
                         e.lost_leader, e.handover_to, self.observing)

    def _member_for_certificate(self, request: web.Request) -> str | None:
        ssl_object = request.transport.get_extra_info("ssl_object") if request.transport else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if der is None or self.config.group is None:
            return None
        presented = fingerprint_of_der(der)
        for virgil_id, member in self.config.group.members.items():
            if fingerprint(member.cert) == presented:
                return virgil_id
        return None

    async def receive_heartbeat(self, request: web.Request) -> web.Response:
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            return web.json_response({"error": "not a member"}, status=403)
        try:
            beat = decode(await request.json())
        except (ValueError, HeartbeatError) as error:
            return web.json_response({"error": str(error)}, status=400)
        if beat.id != sender:
            log.warning("%s sent a heartbeat claiming to be %s", self.name_of(sender), beat.id)
            return web.json_response({"error": "that isn't your Virgil ID"}, status=403)
        self.peers[sender] = Peer(beat, time.monotonic(), request.remote)
        if sender not in self.adverts and request.remote:
            self.learned[sender] = request.remote
        return web.Response(status=204)

    def addresses_for(self, virgil_id: str) -> list[str]:
        advert = self.adverts.get(virgil_id)
        if advert is not None:
            return list(advert.addresses)
        learned = self.learned.get(virgil_id)
        return [learned] if learned else []

    async def send_heartbeats(self) -> None:
        session: aiohttp.ClientSession | None = None
        trusted_for_session: frozenset[str] = frozenset()
        try:
            while True:
                certs = self.member_certs()
                if certs and frozenset(map(fingerprint, certs)) != trusted_for_session:
                    if session is not None:
                        await session.close()
                    context = tls.group_client_context(self.identity, certs)
                    session = aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(ssl=context),
                        timeout=aiohttp.ClientTimeout(total=HEARTBEAT_TIMEOUT),
                    )
                    trusted_for_session = frozenset(map(fingerprint, certs))
                if session is not None:
                    body = self.heartbeat().encode()
                    await asyncio.gather(*(self._send(session, v, body) for v in self.member_ids()))
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        finally:
            if session is not None:
                await session.close()

    async def _send(self, session: aiohttp.ClientSession, virgil_id: str, body: dict) -> None:
        advert = self.adverts.get(virgil_id)
        port = advert.group_port if advert else self.group_port
        for address in self.addresses_for(virgil_id):
            try:
                async with session.post(f"https://{address}:{port}{HEARTBEAT_PATH}", json=body) as reply:
                    if reply.status == 204:
                        return
            except (aiohttp.ClientError, TimeoutError, ssl.SSLError, OSError):
                continue

    # Election

    def run_election_once(self, now: float) -> None:
        views = {i: p.heartbeat.view(p.heard_at) for i, p in self.peers.items() if i in self.member_ids()}
        if self.observing and self.election.state is State.SYNC:
            self._log_change(self.election.synced(now))
        self._log_change(self.election.tick(views, now))

    def _log_change(self, change: Change | None) -> None:
        if change is None:
            return
        old = change.old_state.value + (f" of {self.name_of(change.old_leader)}" if change.old_leader else "")
        new = change.new_state.value + (f" of {self.name_of(change.new_leader)}" if change.new_leader else "")
        prefix = "Would change" if self.observing else "Changing"
        log.info("%s from %s to %s", prefix, old, new)

    async def run_election(self) -> None:
        while True:
            self.run_election_once(time.monotonic())
            await asyncio.sleep(ELECTION_INTERVAL)

    # Discovery

    def on_advert(self, advert: discovery.Advert) -> None:
        if advert.id == self.config.id:
            return
        is_new = advert.id not in self.adverts
        self.adverts[advert.id] = advert
        if is_new:
            member = advert.id in self.member_ids()
            log.info("Found %s at %s%s", advert.name, ", ".join(advert.addresses), " (member)" if member else "")

    def advert_properties(self) -> dict[str, str]:
        group_id = self.config.group.id if self.config.group else None
        return discovery.properties(self.config.id, self.config.name, __version__, group_id,
                                    self.public_port, self.group_port)

    async def start_discovery(self) -> None:
        interface = self.config.mgmt_interface
        if interface is None:
            log.warning("No management interface is set, so other Virgils can't be found")
            return
        addresses = ipv4_addresses(interface)
        if not addresses:
            log.warning("%s has no IPv4 address, so other Virgils can't be found", interface)
            return
        self.discovery = discovery.Discovery(self.on_advert)
        await self.discovery.start(addresses, f"Virgil on {self.config.name}", socket.gethostname(),
                                   self.public_port, self.advert_properties())

    # Public port

    async def status(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        group = self.config.group
        members = []
        for virgil_id in self.member_ids():
            peer = self.peers.get(virgil_id)
            beat = peer.heartbeat if peer else None
            members.append(
                {
                    "id": virgil_id,
                    "name": self.name_of(virgil_id),
                    "state": beat.state.value if beat else None,
                    "leader": self.name_of(beat.leader) if beat else None,
                    "version": beat.version if beat else None,
                    "heard_ago": round(now - peer.heard_at, 1) if peer else None,
                    "addresses": self.addresses_for(virgil_id),
                }
            )
        others = [
            {"id": a.id, "name": a.name, "group_id": a.group_id, "addresses": list(a.addresses)}
            for a in self.adverts.values()
            if a.id not in self.member_ids()
        ]
        return web.json_response(
            {
                "product": "Virgil",
                "version": __version__,
                "id": self.config.id,
                "name": self.config.name,
                "observing": self.observing,
                "state": self.election.state.value,
                "leader": self.name_of(self.election.leader),
                "group": None if group is None else {"id": group.id, "members": members},
                "other_virgils": others,
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
        if request.remote and isinstance(body.get("id"), str):
            self.learned[body["id"]] = request.remote
        log.info("%s joined the group from %s", body.get("name"), request.remote)
        await self._update_advert()
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
        app.router.add_post(HEARTBEAT_PATH, self.receive_heartbeat)
        return app

    async def _update_advert(self) -> None:
        if self.discovery is not None:
            await self.discovery.update(self.advert_properties())

    async def watch_config(self) -> None:
        while True:
            await asyncio.sleep(CONFIG_CHECK_INTERVAL)
            try:
                if self.reload_if_changed():
                    await self._update_advert()
            except Exception:
                log.exception("Couldn't reload the config")


async def serve(daemon: Daemon, host: str) -> None:
    public = web.AppRunner(daemon.public_app(), access_log=None)
    group = web.AppRunner(daemon.group_app(), access_log=None)
    await public.setup()
    await group.setup()
    await web.TCPSite(public, host, daemon.public_port, ssl_context=tls.public_server_context(daemon.identity)).start()
    await web.TCPSite(group, host, daemon.group_port, ssl_context=daemon.group_context).start()
    log.info("Virgil %s: %s (%s), public port %d, group port %d%s",
             __version__, daemon.config.name, daemon.config.id, daemon.public_port, daemon.group_port,
             ", observing the election only" if daemon.observing else "")
    await daemon.start_discovery()
    tasks = [asyncio.create_task(t) for t in (daemon.watch_config(), daemon.send_heartbeats(), daemon.run_election())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if daemon.discovery is not None:
            await daemon.discovery.stop()
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
    daemon = Daemon(args.config, tls.default_identity(args.tls_dir),
                    group_port=args.group_port, public_port=args.public_port)
    asyncio.run(serve(daemon, args.host))
    return 0


if __name__ == "__main__":
    sys.exit(main())
