"""virgild: Virgil's daemon.

It serves the public port (status, and joining a group) and the group port
(members only, by certificate), finds other Virgils over mDNS, exchanges
heartbeats with the members of its group, and runs the leader election.

Once a PTP interface is set, virgild carries out the election's decisions on
the timing services. With observe_only set (or no PTP interface), it only logs
the decisions it would make, and roles are left to `virgilctl role`.
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
from virgil import discovery, ptpwatch, tls
from virgil.config import Config
from virgil.election import Change, Election, State
from virgil.generate import Role
from virgil.group import GROUP_PORT, PUBLIC_PORT
from virgil.heartbeat import HEARTBEAT_PATH, Heartbeat, HeartbeatError, decode
from virgil.interfaces import ipv4_addresses, ipv4_networks
from virgil.request import TimingRequest
from virgil.timing import Target, TimingManager
from virgil.joining import JOIN_PATH, JoinError, handle_join
from virgil.tls import Identity, fingerprint, fingerprint_of_der

log = logging.getLogger("virgild")

CONFIG_CHECK_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 0.8
ELECTION_INTERVAL = 0.5
NETWORK_CHECK_INTERVAL = 2.0
# How long the election waits for the management interface to get an address
# before going ahead anyway, so a console still gets a clock without a network.
NETWORK_WAIT = 60.0


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
    # Whether to carry out the election's decisions. Off for tests and for observe_only.
    manage_timing: bool = True
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
    timing: TimingManager | None = field(default=None, init=False)
    # The management interface's addresses and networks, as last seen.
    addresses: tuple[str, ...] = field(default=(), init=False)
    networks: tuple[str, ...] = field(default=(), init=False)
    # Whether the election is running yet. It waits for the network first.
    electing: bool = field(default=False, init=False)
    started: float = field(default=0.0, init=False)
    ptp: ptpwatch.PtpState = field(default_factory=ptpwatch.PtpState, init=False)
    ptp_watcher: ptpwatch.PtpWatcher | None = field(default=None, init=False)
    ptp_addresses: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        self.config = config_file.load(self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime
        self.group_context = tls.group_server_context(self.identity, [])
        self._trust_members()
        self.started = time.monotonic()
        self.election = Election(self.config.id, self.started)

    @property
    def observing(self) -> bool:
        return self.timing is None

    def start_timing(self, manager: TimingManager | None = None) -> None:
        """Take charge of the timing services, picking up any role they're already running in."""
        if not self.manage_timing or self.config.observe_only:
            log.info("observe_only is set: roles are left to virgilctl")
            return
        if self.config.ptp_interface is None:
            log.warning("No PTP interface is set, so the election is only being observed")
            return
        self.timing = manager or TimingManager(self.build_request, self.on_applied)
        applied = self.timing.applied
        if applied is not None:
            state = State.LEADER if applied.role is Role.LEADER else State.FOLLOWER
            self.election.resume(state, applied.leader_id, time.monotonic())
            log.info("Carrying on as %s", self._describe(state, applied.leader_id))
        elif self.timing.request is not None:
            log.info("The timing services are running; keeping them as they are until the election decides")

    def build_request(self, target: Target) -> TimingRequest | None:
        config = self.config
        if config.ptp_interface is None:
            return None
        networks = list(self.networks)
        if target.role is Role.LEADER:
            if not networks:
                log.warning("The management interface has no network, so no follower can sync to this Virgil")
            return TimingRequest(Role.LEADER, config.ptp_interface, None, tuple(networks),
                                 config.trim_ppb, config.nic_profile is not None)
        addresses = self.addresses_for(target.leader_id) if target.leader_id else []
        if not addresses:
            return None
        leader = addresses[0]
        return TimingRequest(Role.FOLLOWER, config.ptp_interface, leader, tuple(networks or [leader]),
                             config.trim_ppb, config.nic_profile is not None)

    def on_applied(self, target: Target | None) -> None:
        """The timing services now match the election's decision."""
        if (target is not None and target.role is Role.FOLLOWER
                and self.election.state is State.SYNC and self.election.leader == target.leader_id):
            self._log_change(self.election.synced(time.monotonic()))

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
                         e.lost_leader, e.handover_to, self.observing, e.established,
                         self.ptp.own_identity, tuple(self.ptp_warnings()[:8]))

    # PTP watching

    def ptp_warnings(self) -> list[str]:
        members = {}
        for peer in self.peers.values():
            if peer.heartbeat.ptp_identity:
                members[peer.heartbeat.ptp_identity] = peer.heartbeat.name
        return [w[:200] for w in self.ptp.leaks(members)]

    def own_addresses(self) -> set[str]:
        return set(self.addresses) | set(self.ptp_addresses)

    def start_ptp_watch(self) -> None:
        interface = self.config.ptp_interface
        if interface is None:
            return
        self.ptp_addresses = tuple(ipv4_addresses(interface))
        watcher = ptpwatch.PtpWatcher(interface, self.ptp, time.monotonic, self.own_addresses)
        try:
            watcher.start(asyncio.get_running_loop())
        except PermissionError:
            log.warning("Can't watch PTP on %s without CAP_NET_RAW", interface)
            return
        except OSError as error:
            log.warning("Can't watch PTP on %s: %s", interface, error)
            return
        self.ptp_watcher = watcher

    async def tidy_ptp(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            self.ptp.forget_old(time.monotonic())
            if self.config.ptp_interface:
                self.ptp_addresses = tuple(ipv4_addresses(self.config.ptp_interface))

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
        change = self.election.tick(views, now)
        self._log_change(change)
        if change is not None and self.timing is not None:
            target = self._target(change.new_state, change.new_leader)
            if target is not False:
                self.timing.want(target)

    @staticmethod
    def _target(state: State, leader: str | None) -> Target | None | bool:
        """What the timing services should run for an election state. False means leave them alone."""
        if state is State.LEADER:
            return Target(Role.LEADER)
        if state in (State.SYNC, State.FOLLOWER):
            return Target(Role.FOLLOWER, leader)
        if state is State.STOPPED:
            return None
        return False  # Listening never stops anything.

    def _describe(self, state: State, leader: str | None) -> str:
        return state.value + (f" of {self.name_of(leader)}" if leader else "")

    def _log_change(self, change: Change | None) -> None:
        if change is None:
            return
        prefix = "Would change" if self.observing else "Changing"
        log.info("%s from %s to %s", prefix, self._describe(change.old_state, change.old_leader),
                 self._describe(change.new_state, change.new_leader))

    def ready_to_elect(self, now: float) -> bool:
        """Whether the election should run yet.

        A Virgil that can't reach the network can't hear any other member, so
        it would wrongly decide it's alone. The election waits until the
        management interface has an address, or NETWORK_WAIT has passed.
        """
        if self.electing:
            return True
        if self.addresses:
            log.info("%s is ready; starting the election", self.config.mgmt_interface)
        elif now - self.started >= NETWORK_WAIT:
            log.warning("Still no management network after %.0f s; going ahead without it", NETWORK_WAIT)
        else:
            return False
        self.electing = True
        self.election.listen_from(now)
        return True

    async def run_election(self) -> None:
        while True:
            now = time.monotonic()
            if self.ready_to_elect(now):
                self.run_election_once(now)
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

    async def check_network(self) -> None:
        """Follow the management interface's addresses, (re)starting discovery when they change."""
        interface = self.config.mgmt_interface
        addresses = tuple(ipv4_addresses(interface)) if interface else ()
        networks = tuple(ipv4_networks(interface)) if interface else ()
        # The networks go first: the election starts as soon as there's an
        # address, and the first role it sets up should already allow them.
        networks_changed = networks != self.networks
        self.networks = networks
        if addresses != self.addresses:
            if self.discovery is not None:
                await self.discovery.stop()
                self.discovery = None
            self.addresses = addresses
            if addresses:
                log.info("%s is at %s", interface, ", ".join(addresses))
                self.discovery = discovery.Discovery(self.on_advert)
                await self.discovery.start(list(addresses), f"Virgil on {self.config.name}",
                                           socket.gethostname(), self.public_port, self.advert_properties())
            elif interface:
                log.warning("%s has no IPv4 address yet, so other Virgils can't be found", interface)
            else:
                log.warning("No management interface is set, so other Virgils can't be found")
        if networks_changed and self.timing is not None:
            self.timing.poke()

    async def watch_network(self) -> None:
        while True:
            try:
                await self.check_network()
            except Exception:
                log.exception("Couldn't check the management interface")
            await asyncio.sleep(NETWORK_CHECK_INTERVAL)

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
                    "ptp_identity": beat.ptp_identity if beat else None,
                    "ptp_warnings": list(beat.ptp_warnings) if beat else [],
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
                "waiting_for_network": not self.electing,
                "state": self.election.state.value,
                "leader": self.name_of(self.election.leader),
                "group": None if group is None else {"id": group.id, "members": members},
                "ptp": {
                    "watching": self.ptp_watcher is not None,
                    "identity": self.ptp.own_identity,
                    "local_devices": [
                        {
                            "address": d.address,
                            "ptpv1": d.ptpv1,
                            "follows_this_virgil": d.follows_this_virgil,
                            "seen_ago": round(now - d.last_seen, 1),
                        }
                        for d in sorted(self.ptp.devices.values(), key=lambda d: d.address)
                    ],
                    "warnings": self.ptp_warnings(),
                },
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
                    if self.timing is not None:
                        self.timing.poke()
            except Exception:
                log.exception("Couldn't reload the config")


async def serve(daemon: Daemon, host: str) -> None:
    public = web.AppRunner(daemon.public_app(), access_log=None)
    group = web.AppRunner(daemon.group_app(), access_log=None)
    await public.setup()
    await group.setup()
    await web.TCPSite(public, host, daemon.public_port, ssl_context=tls.public_server_context(daemon.identity)).start()
    await web.TCPSite(group, host, daemon.group_port, ssl_context=daemon.group_context).start()
    log.info("Virgil %s: %s (%s), public port %d, group port %d",
             __version__, daemon.config.name, daemon.config.id, daemon.public_port, daemon.group_port)
    await daemon.check_network()
    daemon.start_timing()
    daemon.start_ptp_watch()
    jobs = [daemon.watch_config(), daemon.watch_network(), daemon.send_heartbeats(), daemon.run_election(),
            daemon.tidy_ptp()]
    if daemon.timing is not None:
        jobs.append(daemon.timing.run())
    tasks = [asyncio.create_task(job) for job in jobs]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if daemon.ptp_watcher is not None:
            daemon.ptp_watcher.stop(asyncio.get_running_loop())
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
