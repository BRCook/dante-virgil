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
import json
import logging
import os
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
from virgil import autotrim, clocks, discovery, ptpwatch, roles, tls
from virgil.consoles import POLL_INTERVAL, Consoles, NetaudioClient
from virgil.control import CONTROL_SOCKET, HANDOVER_PATH, LEAD_PATH
from virgil.config import Config
from virgil.files import atomic_write
from virgil.election import Change, Election, State
from virgil.generate import Role
from virgil.group import GROUP_PORT, PUBLIC_PORT
from virgil.heartbeat import HEARTBEAT_PATH, Heartbeat, HeartbeatError, decode
from virgil.interfaces import Timestamping, get_interface, ipv4_addresses, ipv4_networks, is_link_local
from virgil.request import TimingRequest
from virgil.timing import Target, TimingManager
from virgil.joining import JOIN_PATH, JoinError, handle_join
from virgil.tls import Identity, fingerprint, fingerprint_of_der
from virgil.webapi import WebApi
from virgil.webextra import HTTP_PORT, HTTPS_PORT, NETAUDIO_PROXY_PORT, netaudio_app, redirect_app

log = logging.getLogger("virgild")

WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = {"style.css", "app.js"}
# The page may only load its own files, and nothing may frame it.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

CONFIG_CHECK_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 0.8
ELECTION_INTERVAL = 0.5
NETWORK_CHECK_INTERVAL = 2.0
# How long the election waits for the management interface to get an address
# before going ahead anyway, so a console still gets a clock without a network.
NETWORK_WAIT = 60.0
# How long a self-assigned 169.254.x.x address has to be the only one before
# it counts, so DHCP has time to finish on networks that have it.
LINK_LOCAL_GRACE = 30.0
# Exiting with this has systemd start virgild again (Restart=on-failure).
RESTART_EXIT = 75
NETAUDIO_UNIT = "dante-virgil-netaudio.service"
DHCP_NAMES_UNIT = "dante-virgil-dhcpnames.service"
DHCP_NAMES_REQUEST = Path("/run/dante-virgil/dhcp-names.json")
NETAUDIO_ENV = Path("/run/dante-virgil/netaudio.env")


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
    web: WebApi | None = field(default=None, init=False)
    # Set to have virgild shut down cleanly and exit with RESTART_EXIT, so systemd starts it again.
    restart: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    # The management interface's addresses and networks, as last seen.
    addresses: tuple[str, ...] = field(default=(), init=False)
    networks: tuple[str, ...] = field(default=(), init=False)
    # Whether the election is running yet. It waits for the network first.
    electing: bool = field(default=False, init=False)
    started: float = field(default=0.0, init=False)
    ptp: ptpwatch.PtpState = field(default_factory=ptpwatch.PtpState, init=False)
    ptp_watcher: ptpwatch.PtpWatcher | None = field(default=None, init=False)
    ptp_addresses: tuple[str, ...] = field(default=(), init=False)
    consoles: Consoles | None = field(default=None, init=False)
    # When the management interface's only addresses became link-local ones.
    link_local_since: float | None = field(default=None, init=False)
    # The PTP interface addresses netaudio's daemon was started for, or None if it's
    # stopped. It starts unknown, so the first check always starts or stops it.
    netaudio_for: tuple[str, ...] | None = field(default=("unknown",), init=False)
    systemd: roles.Systemd | None = field(default=None, init=False)
    clock: dict | None = field(default=None, init=False)
    # The interfaces whose DHCP lease names were last set, so they're only set again on a change.
    dhcp_names_for: tuple | None = field(default=None, init=False)
    # What auto-trim last did, for status.
    last_trim: dict | None = field(default=None, init=False)
    # (trim applied, consoles' average) for recent steps, and why auto-trim paused, if it has.
    trim_history: list = field(default_factory=list, init=False)
    trim_paused: str | None = field(default=None, init=False)

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

    def ntp_hwtimestamp_interface(self, lookup=get_interface) -> str | None:
        """The management interface, if chrony can use its hardware timestamps.

        Never on the NIC ptp4l uses: not the same interface, and not another
        port sharing its hardware clock. Hardware timestamps are only ever
        used by one program per NIC.
        """
        config = self.config
        if not config.ntp_hwtimestamp or config.mgmt_interface is None:
            return None
        if config.mgmt_interface == config.ptp_interface:
            return None
        interface = lookup(config.mgmt_interface)
        if interface is None or not interface.physical or interface.mode is not Timestamping.HARDWARE:
            return None
        ptp = lookup(config.ptp_interface) if config.ptp_interface else None
        if ptp is not None and ptp.timestamping is not None and interface.timestamping is not None \
                and ptp.timestamping.phc_index >= 0 \
                and ptp.timestamping.phc_index == interface.timestamping.phc_index:
            return None  # another port on the same card, sharing its hardware clock
        return config.mgmt_interface

    def build_request(self, target: Target) -> TimingRequest | None:
        config = self.config
        if config.ptp_interface is None:
            return None
        networks = list(self.networks)
        if target.role is Role.LEADER:
            if not networks:
                log.warning("The management interface has no network, so no follower can sync to this Virgil")
            return TimingRequest(Role.LEADER, config.ptp_interface, None, tuple(networks),
                                 config.trim_ppb, config.nic_profile is not None,
                                 ntp_hwtimestamp=self.ntp_hwtimestamp_interface())
        addresses = self.addresses_for(target.leader_id) if target.leader_id else []
        if not addresses:
            return None
        leader = addresses[0]
        return TimingRequest(Role.FOLLOWER, config.ptp_interface, leader, tuple(networks or [leader]),
                             config.trim_ppb, config.nic_profile is not None,
                             ntp_hwtimestamp=self.ntp_hwtimestamp_interface())

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
        old_password = self.config.web_password
        self.config = config_file.load(self.config_path)
        if self.web is not None and self.config.web_password != old_password:
            self.web.sessions.end_all()
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
        devices = tuple(self.consoles.summaries()[:16]) if self.consoles else ()
        return Heartbeat(self.config.id, self.config.name, __version__, e.state, e.coin, e.leader,
                         e.lost_leader, e.handover_to, self.observing, e.established,
                         self.ptp.own_identity, tuple(self.ptp_warnings()[:8]), devices, self.clock)

    async def watch_clock(self) -> None:
        while True:
            try:
                follower = self.election.state in (State.FOLLOWER, State.SYNC)
                self.clock = await clocks.read_clock(follower, None if follower else self.config.trim_ppb) or None
            except Exception:
                log.exception("Couldn't read the clocks")
            await asyncio.sleep(5.0)

    # Local Dante devices, through netaudio

    def start_consoles(self, client: NetaudioClient | None = None, systemd: roles.Systemd | None = None) -> None:
        """Watch the local Dante devices through netaudio's daemon.

        The daemon itself is started by check_ptp_interface, once the PTP
        interface has an address: netaudio won't run without one.
        """
        interface = self.config.ptp_interface
        if interface is None or not self.manage_timing:
            return
        if client is None:
            netaudio = Path(sys.prefix) / "bin" / "netaudio"
            if not netaudio.exists():
                log.info("netaudio isn't installed, so Dante devices aren't being monitored")
                return
            self.systemd = systemd or roles.Systemd()
        local = lambda: {a: d.follows_this_virgil for a, d in self.ptp.devices.items()}  # noqa: E731
        def clock_identity(address: str) -> str | None:
            device = self.ptp.devices.get(address)
            return device.clock_identity if device else None

        def member_identities() -> dict[str, str]:
            return {p.heartbeat.ptp_identity: p.heartbeat.name for p in self.peers.values()
                    if p.heartbeat.ptp_identity}

        self.consoles = Consoles(client or NetaudioClient(), local, lambda: self.config.devices,
                                 clock_identity=clock_identity, own_identity=lambda: self.ptp.own_identity,
                                 member_identities=member_identities)

    def check_ptp_interface(self) -> None:
        """Follow the PTP interface's addresses, and run netaudio's daemon only while it has one."""
        interface = self.config.ptp_interface
        if interface is None:
            return
        # Dante devices may well be link-local, so every address counts here.
        self.ptp_addresses = tuple(ipv4_addresses(interface, link_local=True))
        if self.systemd is None:
            return
        wanted = self.ptp_addresses or None
        if wanted == self.netaudio_for:
            return
        try:
            if wanted is None:
                log.info("%s has no IPv4 address, so netaudio's daemon is stopped until it does", interface)
                self.systemd.stop(NETAUDIO_UNIT)
            else:
                NETAUDIO_ENV.write_text(f"NETAUDIO_INTERFACE={interface}\n")
                self.systemd.restart(NETAUDIO_UNIT)
                log.info("Started netaudio's daemon on %s", interface)
        except (OSError, roles.ServiceError) as error:
            log.warning("Couldn't control netaudio's daemon: %s", error)
            return
        self.netaudio_for = wanted

    @property
    def netaudio_running(self) -> bool:
        """Whether netaudio's daemon should be running. Without systemd (in tests), it's assumed to be."""
        if self.systemd is None:
            return True
        return self.netaudio_for not in (None, ("unknown",))

    async def watch_consoles(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if self.consoles is not None and not self.netaudio_running:
                # Stopped on purpose, so there's nothing to poll or warn about.
                self.consoles.devices.clear()
                self.consoles.reachable = None
                continue
            if self.consoles is not None:
                try:
                    await self.consoles.poll()
                except Exception:
                    log.exception("Couldn't check the local Dante devices")

    # PTP watching

    def ptp_warnings(self) -> list[str]:
        members = {}
        for peer in self.peers.values():
            if peer.heartbeat.ptp_identity:
                members[peer.heartbeat.ptp_identity] = peer.heartbeat.name
        return [w[:200] for w in self.ptp.leaks(members)]

    def _ptp_link_up(self) -> bool | None:
        if self.config.ptp_interface is None:
            return None
        interface = get_interface(self.config.ptp_interface, timestamping=lambda name: None)
        return None if interface is None else interface.link_up

    def own_addresses(self) -> set[str]:
        return set(self.addresses) | set(self.ptp_addresses)

    def start_ptp_watch(self) -> None:
        interface = self.config.ptp_interface
        if interface is None:
            return
        self.ptp_addresses = tuple(ipv4_addresses(interface, link_local=True))
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
        if change is not None and change.new_state is State.LEADER and change.old_state is State.FOLLOWER:
            self.adopt_phc_rate()
        if change is not None and self.timing is not None:
            target = self._target(change.new_state, change.new_leader)
            if target is not False:
                self.timing.want(target)

    # Automatic trim

    def console_offsets(self, now: float) -> list[int]:
        """The clock offsets of every console in the group that's following its Virgil."""
        devices = list(self.consoles.summaries()) if self.consoles else []
        for virgil_id in self.member_ids():
            peer = self.peers.get(virgil_id)
            if peer is not None and now - peer.heard_at <= self.election.timing.visible:
                devices += list(peer.heartbeat.dante_devices)
        return [
            d["clock_offset_ppb"] for d in devices
            if d.get("follows_this_virgil") and isinstance(d.get("clock_offset_ppb"), int)
        ]

    def trim_once(self, now: float) -> None:
        """One auto-trim step, if this Virgil is leading and has consoles to go on."""
        if not self.config.auto_trim:
            self.trim_paused = None  # Turning it off and on again resumes it.
            self.trim_history.clear()
            return
        if self.timing is None or self.election.state is not State.LEADER or self.trim_paused:
            return
        if now - self.election.since < autotrim.INTERVAL:
            self.trim_history.clear()
            return  # Let things settle after taking the lead.
        request = self.timing.request
        if request is None or request.trim_ppb != self.config.trim_ppb:
            return  # The last trim hasn't been applied yet.
        offsets = autotrim.plausible(self.console_offsets(now))
        if not offsets:
            return
        average = sum(offsets) / len(offsets)
        self.trim_history = (self.trim_history + [(self.config.trim_ppb, average)])[-4:]
        if autotrim.unresponsive(self.trim_history):
            first_trim, first_average = self.trim_history[0]
            self.trim_paused = (
                f"the trim moved {(self.config.trim_ppb - first_trim) / 1000:+.3f} ppm but the consoles only "
                f"{(average - first_average) / 1000:+.3f} ppm"
            )
            log.warning("Auto-trim paused: %s. Restart virgild, or turn auto_trim off and on, to resume",
                        self.trim_paused)
            return
        new = autotrim.next_trim(self.config.trim_ppb, offsets)
        if new is None:
            return
        old = self.config.trim_ppb
        log.info("Auto-trim: consoles average %+.3f ppm, so the trim goes from %+.3f to %+.3f ppm",
                 average / 1000, old / 1000, new / 1000)
        self.config.trim_ppb = new
        try:
            self.save()
        except OSError as error:
            log.warning("Couldn't save the new trim: %s", error)
        self.timing.poke()
        self.last_trim = {"at": time.time(), "from_ppb": old, "to_ppb": new, "consoles_ppb": round(average)}

    async def watch_trim(self) -> None:
        while True:
            await asyncio.sleep(autotrim.INTERVAL)
            try:
                self.trim_once(time.monotonic())
            except Exception:
                log.exception("Auto-trim failed")

    def adopt_phc_rate(self) -> None:
        """On taking the lead, make the PHC's current rate this Virgil's trim.

        The PHC keeps the rate phc2sys last gave it, which is the old leader's
        rate as closely as this Virgil could measure it. Its trim has to match,
        or the next trim change would jump the PHC back to an old figure.
        """
        adjustment = (self.clock or {}).get("phc_adjustment_ppm")
        if not isinstance(adjustment, (int, float)) or isinstance(adjustment, bool):
            return
        trim = round(adjustment * 1000)
        if trim == self.config.trim_ppb:
            return
        log.info("Taking over the PHC's current rate as this Virgil's trim: %+.3f ppm", trim / 1000)
        self.config.trim_ppb = trim
        try:
            self.save()
        except OSError as error:
            log.warning("Couldn't save the new trim: %s", error)

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

    @property
    def mdns_host(self) -> str:
        """The host name in this Virgil's mDNS advert.

        It isn't the machine's hostname: netaudio's daemon publishes that on the
        PTP interface, and a lookup would then return the PTP interface's
        address as well as the management interface's. Nothing else publishes
        this name, so it only ever resolves to the management interface.
        """
        return f"virgil-{self.config.id[:12]}"

    async def check_network(self) -> None:
        """Follow the management interface's addresses, (re)starting discovery when they change."""
        interface = self.config.mgmt_interface
        every = tuple(ipv4_addresses(interface, link_local=True)) if interface else ()
        addresses = tuple(a for a in every if not is_link_local(a))
        networks = tuple(ipv4_networks(interface)) if interface else ()
        if addresses or not every:
            self.link_local_since = None
        else:
            # Only self-assigned addresses. They count once DHCP has had its chance.
            now = time.monotonic()
            if self.link_local_since is None:
                self.link_local_since = now
            if now - self.link_local_since >= LINK_LOCAL_GRACE:
                addresses = every
                networks = tuple(n for n in ipv4_networks(interface, link_local=True) if n.startswith("169.254."))
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
                                           self.mdns_host, self.public_port, self.advert_properties())
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
                self.check_ptp_interface()
            except Exception:
                log.exception("Couldn't check the network interfaces")
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
                    "dante_devices": list(beat.dante_devices) if beat else [],
                    "clock": beat.clock if beat else None,
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
                "addresses": list(self.addresses),
                "observing": self.observing,
                "waiting_for_network": not self.electing,
                "state": self.election.state.value,
                "leader": self.name_of(self.election.leader),
                "group": None if group is None else {"id": group.id, "members": members},
                "clock": self.clock,
                "dhcp_names": dhcp_lease_names(),
                "trim_ppb": self.config.trim_ppb,
                "ntp_hwtimestamp": (self.timing.request.ntp_hwtimestamp
                                    if self.timing is not None and self.timing.request is not None else None),
                "auto_trim": {"enabled": self.config.auto_trim, "last": self.last_trim, "paused": self.trim_paused},
                "ptp": {
                    "interface": self.config.ptp_interface,
                    "link_up": self._ptp_link_up(),
                    "has_address": bool(self.ptp_addresses),
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
                "netaudio": None if self.consoles is None else {
                    "running": self.netaudio_running,
                    "reachable": self.consoles.reachable,
                    "ui_port": NETAUDIO_PROXY_PORT if self.systemd is not None else None,
                },
                "dante_devices": self.consoles.summaries() if self.consoles else [],
                "device_settings": {
                    mac: {"native_ptpv2": d.native_ptpv2, "aes67": d.aes67} for mac, d in self.config.devices.items()
                },
                "diagnostics": self.consoles.diagnostics if self.consoles else {},
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
        app = web.Application(client_max_size=64 * 1024, middlewares=[security_headers])
        app.router.add_get("/", self.page)
        app.router.add_get("/static/{name}", self.static_file)
        app.router.add_get("/api/v1/status", self.status)
        app.router.add_post(JOIN_PATH, self.join)
        self.web = WebApi(self)
        self.web.routes(app)
        return app

    # For the web UI

    async def page(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB_DIR / "index.html")

    async def static_file(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in STATIC_FILES:
            raise web.HTTPNotFound()
        return web.FileResponse(WEB_DIR / name)

    def identity_fingerprint(self) -> str:
        return fingerprint(self.identity.cert_pem)

    def group_changed(self) -> None:
        """After creating or joining a group: trust the new members, and advertise the group."""
        self._trust_members()
        if self.discovery is not None:
            asyncio.ensure_future(self._update_advert())

    def update_dhcp_names(self, systemd: roles.Systemd | None = None, path: Path = DHCP_NAMES_REQUEST) -> None:
        """Ask virgil-root to name each chosen interface's DHCP lease, if the interfaces have changed."""
        if not self.manage_timing:
            return
        wanted = (self.config.ptp_interface, self.config.mgmt_interface)
        if wanted == self.dhcp_names_for:
            return
        try:
            atomic_write(path, json.dumps({"ptp_interface": wanted[0], "mgmt_interface": wanted[1]}).encode(),
                         mode=0o640)
            (systemd or roles.Systemd()).start(DHCP_NAMES_UNIT, block=False)
        except (OSError, roles.ServiceError) as error:
            log.warning("Couldn't update the DHCP lease names: %s", error)
            return
        self.dhcp_names_for = wanted

    def request_restart(self, delay: float = 1.0) -> None:
        """Restart virgild shortly, once the current reply has gone out.

        virgild can't restart its own unit through polkit, on purpose, so it
        exits instead, and systemd's Restart=on-failure starts it again.
        Restarting never disturbs the timing services.
        """
        asyncio.get_running_loop().call_later(delay, self.restart.set)

    def stop_timing(self) -> bool:
        """Stop this Virgil's timing services, until started again. False if roles aren't managed."""
        if self.timing is None:
            return False
        self._log_change(self.election.stop(time.monotonic()))
        self.timing.want(None)
        return True

    def start_timing_again(self) -> bool:
        if self.timing is None:
            return False
        self._log_change(self.election.start(time.monotonic()))
        return True

    # Group port

    async def ping(self, request: web.Request) -> web.Response:
        return web.json_response({"id": self.config.id, "name": self.config.name})

    async def receive_handover(self, request: web.Request) -> web.Response:
        """A member asks this Virgil, the leader, to hand leadership over to it."""
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            return web.json_response({"error": "not a member"}, status=403)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "the request must be JSON"}, status=400)
        if not isinstance(body, dict) or body.get("to") != sender:
            # A member can only ask for leadership for itself.
            return web.json_response({"error": "a member can only ask to lead itself"}, status=403)
        if self.election.state is not State.LEADER:
            return web.json_response({"error": f"{self.config.name} isn't leading"}, status=409)
        self.election.hand_over(sender)
        log.info("Handing leadership over to %s, at its request", self.name_of(sender))
        return web.json_response({"handing_over_to": sender}, status=202)

    def group_app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/group/v1/ping", self.ping)
        app.router.add_post(HEARTBEAT_PATH, self.receive_heartbeat)
        app.router.add_post(HANDOVER_PATH, self.receive_handover)
        return app

    # Local control

    async def request_lead(self) -> tuple[int, dict]:
        """Ask the current leader to hand leadership over to this Virgil."""
        if self.timing is None:
            return 409, {"error": "virgild is only observing, so it can't take the lead"}
        state, leader = self.election.state, self.election.leader
        if state is State.LEADER:
            return 200, {"message": f"{self.config.name} is already leading"}
        if state is not State.FOLLOWER or leader is None:
            return 409, {"error": f"{self.config.name} is {state.value}; it can only take the lead as a follower"}
        context = tls.group_client_context(self.identity, self.member_certs())
        advert = self.adverts.get(leader)
        port = advert.group_port if advert else self.group_port
        errors = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            for address in self.addresses_for(leader):
                try:
                    url = f"https://{address}:{port}{HANDOVER_PATH}"
                    async with session.post(url, json={"to": self.config.id}, ssl=context) as reply:
                        data = await reply.json(content_type=None)
                        if reply.status == 202:
                            log.info("%s agreed to hand leadership over", self.name_of(leader))
                            return 202, {"message": f"{self.name_of(leader)} is handing leadership over to "
                                                    f"{self.config.name}"}
                        return reply.status, {"error": (data or {}).get("error", f"refused ({reply.status})")}
                except (aiohttp.ClientError, TimeoutError, ssl.SSLError, OSError) as error:
                    errors.append(f"{address}: {error or type(error).__name__}")
        return 503, {"error": f"couldn't reach {self.name_of(leader)} (" + "; ".join(errors or ["no address"]) + ")"}

    async def control_lead(self, request: web.Request) -> web.Response:
        status, body = await self.request_lead()
        return web.json_response(body, status=status)

    def control_app(self) -> web.Application:
        app = web.Application(client_max_size=4096)
        app.router.add_post(LEAD_PATH, self.control_lead)
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
                    self.update_dhcp_names()
                    if self.timing is not None:
                        self.timing.poke()
            except Exception:
                log.exception("Couldn't reload the config")


def dhcp_lease_names() -> dict[str, str]:
    """The DHCP lease name each kind of interface asks for."""
    from virgil.dhcpnames import host_label

    hostname = socket.gethostname()
    return {"ptp": host_label(hostname, "ptp"), "mgmt": host_label(hostname, "mgmt")}


async def start_extras(daemon: Daemon, host: str, public: web.AppRunner) -> list[web.AppRunner]:
    """The web UI on 443, a redirect from HTTP on 80, and netaudio's UI. None of them is essential."""
    runners: list[web.AppRunner] = []
    context = tls.public_server_context(daemon.identity)
    try:
        await web.TCPSite(public, host, HTTPS_PORT, ssl_context=context).start()
    except OSError as error:
        log.warning("Couldn't serve the web UI on port %d: %s", HTTPS_PORT, error)
    redirect = web.AppRunner(redirect_app(), access_log=None)
    await redirect.setup()
    runners.append(redirect)
    try:
        await web.TCPSite(redirect, host, HTTP_PORT).start()
    except OSError as error:
        log.warning("Couldn't redirect HTTP on port %d: %s", HTTP_PORT, error)
    if daemon.systemd is not None:  # netaudio is installed
        def logged_in(token: str | None) -> bool:
            return daemon.web is not None and daemon.web.sessions.valid(token)

        netaudio = web.AppRunner(netaudio_app(logged_in), access_log=None)
        await netaudio.setup()
        runners.append(netaudio)
        try:
            await web.TCPSite(netaudio, host, NETAUDIO_PROXY_PORT, ssl_context=context).start()
        except OSError as error:
            log.warning("Couldn't serve netaudio's UI on port %d: %s", NETAUDIO_PROXY_PORT, error)
    return runners


async def serve(daemon: Daemon, host: str) -> None:
    public = web.AppRunner(daemon.public_app(), access_log=None)
    group = web.AppRunner(daemon.group_app(), access_log=None)
    await public.setup()
    await group.setup()
    await web.TCPSite(public, host, daemon.public_port, ssl_context=tls.public_server_context(daemon.identity)).start()
    await web.TCPSite(group, host, daemon.group_port, ssl_context=daemon.group_context).start()
    control = web.AppRunner(daemon.control_app(), access_log=None)
    await control.setup()
    try:
        CONTROL_SOCKET.unlink(missing_ok=True)
        await web.UnixSite(control, str(CONTROL_SOCKET)).start()
        os.chmod(CONTROL_SOCKET, 0o660)
    except OSError as error:
        log.warning("Couldn't open the control socket: %s", error)
    log.info("Virgil %s: %s (%s), public port %d, group port %d",
             __version__, daemon.config.name, daemon.config.id, daemon.public_port, daemon.group_port)
    await daemon.check_network()
    daemon.start_timing()
    daemon.start_ptp_watch()
    daemon.start_consoles()
    daemon.update_dhcp_names()
    extras = await start_extras(daemon, host, public)
    jobs = [daemon.watch_config(), daemon.watch_network(), daemon.send_heartbeats(), daemon.run_election(),
            daemon.tidy_ptp(), daemon.watch_consoles(), daemon.watch_clock(), daemon.watch_trim()]
    if daemon.timing is not None:
        jobs.append(daemon.timing.run())
    tasks = [asyncio.create_task(job) for job in jobs]
    waiting = asyncio.create_task(daemon.restart.wait())
    try:
        await asyncio.wait([*tasks, waiting], return_when=asyncio.FIRST_COMPLETED)
        if daemon.restart.is_set():
            log.info("Restarting")
        else:
            for task in tasks:
                if task.done() and task.exception():
                    raise task.exception()
    finally:
        waiting.cancel()
        for task in tasks:
            task.cancel()
        if daemon.ptp_watcher is not None:
            daemon.ptp_watcher.stop(asyncio.get_running_loop())
        if daemon.discovery is not None:
            await daemon.discovery.stop()
        for runner in extras:
            await runner.cleanup()
        await public.cleanup()
        await group.cleanup()
        await control.cleanup()


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
    return RESTART_EXIT if daemon.restart.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
