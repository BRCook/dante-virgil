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
from virgil import autotrim, clocks, discovery, ptpwatch, roles, tls, updates
from virgil.alerts import EVENTS_PATH, Alerts
from virgil.events import EventLog
from virgil.graphs import KEEP_SECONDS, WORST, Recorder, jitter
from virgil.pinger import Pinger
from virgil.replay import Replays
from virgil.recording import ARCHIVE, RECORDINGS, Session, stamp_now, summary_of, summary_path
from virgil import ptpv1 as ptpv1_module
from virgil import timestamping
from virgil.ptpwatch import mac_of_identity
from virgil.consoles import POLL_INTERVAL, Consoles, NetaudioClient
from virgil.control import CONTROL_SOCKET, HANDOVER_PATH, LEAD_PATH, LEAVE_PATH, REMOVE_PATH
from virgil import membership
from virgil import shared
from virgil.config import Config, DeviceSettings
from virgil.consoles import normalise_mac
from virgil.files import atomic_write
from virgil.election import Change, Election, State
from virgil.generate import Tuning, Role
from virgil.group import GROUP_PORT, HEX_ID, PUBLIC_PORT, GroupError, outstanding_invites
from virgil.heartbeat import HEARTBEAT_PATH, Heartbeat, HeartbeatError, decode
from virgil.interfaces import Timestamping, get_interface, ipv4_addresses, ipv4_networks, is_link_local
from virgil.request import MAX_TRIM_PPB, TimingRequest
from virgil.tasks import spawn
from virgil.timing import Target, TimingManager
from virgil.joining import JOIN_PATH, JoinError, handle_join
from virgil.tls import Identity, fingerprint, fingerprint_of_der
from virgil.webapi import WebApi
from virgil.webextra import HTTP_PORT, HTTPS_PORT, NETAUDIO_PROXY_PORT, netaudio_app, redirect_app

log = logging.getLogger("virgild")

WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = {"style.css", "app.js", "graphs.js", "replay.js"}
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
# How long the answer to "should this Virgil serve PTPv1?" must hold before it switches.
PTPV1_SETTLE = 15.0
# How long a self-assigned 169.254.x.x address has to be the only one before
# it counts, so DHCP has time to finish on networks that have it.
LINK_LOCAL_GRACE = 30.0
# Exiting with this has systemd start virgild again (Restart=on-failure).
RESTART_EXIT = 75
# Seconds without any PTP from outside this Virgil before it says so.
PTP_QUIET_AFTER = 60.0
GRAPHS_PATH = "/group/v1/graphs"
DEVICE_PATH = "/group/v1/device"
MEMBERS_PATH = "/group/v1/members"
# Dante marks its audio EF (46); pings across the Dante network are marked the same, to share its queues.
DANTE_AUDIO_DSCP = 46
MEMBERSHIP_PATH = "/api/v1/membership"
# Fetch a member's list at most this often, when its digest differs from ours.
MEMBERS_FETCH_EVERY = 5.0
# With no member heard for this long, ask them (on their public ports) whether this Virgil was removed.
REMOVED_CHECK_AFTER = 60.0
SETTIME_PATH = "/group/v1/settime"
RESYNC_PATH = "/group/v1/resync"
SETTIME_UNIT = "dante-virgil-settime.service"
SETTIME_REQUEST = Path("/run/dante-virgil/settime.json")
RECORDING_PATH = "/group/v1/recording"
RECORDINGS_PATH = "/group/v1/recordings"
# A member sends its log across the link at no more than this, in bytes a second, in chunks of LOG_CHUNK.
LOG_RATE = 2 * 1024 * 1024
LOG_CHUNK = 64 * 1024
# Members get this long to stop and pack a session, and to send it: generous, since a long one takes a while.
LOG_PACK_TIMEOUT = 900.0
LOG_SEND_TIMEOUT = 1800.0
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
    # The address each member was last heard from, by Virgil ID: where its latest heartbeat (or its joining) came
    # from, over a connection only that member could have made. These are the addresses Virgil trusts; mDNS
    # adverts, which anyone on the network can send, only help to find a member in the first place.
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
    ptp_watch_started: float = field(default=0.0, init=False)
    consoles: Consoles | None = field(default=None, init=False)
    # When the management interface's only addresses became link-local ones.
    link_local_since: float | None = field(default=None, init=False)
    # The PTP interface addresses netaudio's daemon was started for, or None if it's
    # stopped. It starts unknown, so the first check always starts or stops it.
    netaudio_for: tuple[str, ...] | None = field(default=("unknown",), init=False)
    systemd: roles.Systemd | None = field(default=None, init=False)
    clock: dict | None = field(default=None, init=False)
    # netaudio's UI: its app, and where it's listening (address to site).
    netaudio_ui: web.AppRunner | None = field(default=None, init=False)
    netaudio_ui_context: ssl.SSLContext | None = field(default=None, init=False)
    netaudio_ui_sites: dict = field(default_factory=dict, init=False)
    alerts: Alerts | None = field(default=None, init=False)
    graphs: Recorder = field(default_factory=Recorder, init=False)
    # The link to each member, from pings: address to (last round trip, jitter), and late-packet counts.
    link: dict = field(default_factory=dict, init=False)
    late_counts: dict = field(default_factory=dict, init=False)
    _has_history: dict = field(default_factory=dict, init=False)
    recording: Session | None = field(default=None, init=False)
    recordings_root: Path = field(default=RECORDINGS, init=False)
    # The members this Virgil started recording, as the one coordinating a group recording.
    recording_members: list = field(default_factory=list, init=False)
    recording_here: bool = field(default=False, init=False)
    # The imported web certificate's details, if there is one.
    web_certificate: dict | None = field(default=None, init=False)
    # Serving PTPv1: the grandmaster when it's running, why it is or isn't, and a change waiting to settle.
    ptpv1_server: ptpv1_module.Server | None = field(default=None, init=False)
    ptpv1_reason: str = field(default="watching the network before deciding", init=False)
    ptpv1_pending: tuple | None = field(default=None, init=False)
    # Whether ptp4l should serve PTPv2. Off in the "only" PTPv1 mode; switched in step with PTPv1 (see switch_ptpv1_mode).
    serve_ptpv2_wanted: bool | None = field(default=None, init=False)
    ptpv1_switching: asyncio.Task | None = field(default=None, init=False)
    # Keeping the member list in step (see virgil.membership).
    _members_fetched: dict = field(default_factory=dict, init=False)
    # Alerts and the group's events, in one history (see virgil.events).
    _event_log: EventLog | None = field(default=None, init=False)
    _timestamping_seen: tuple | None = field(default=None, init=False)
    # Auto-trim's history of the Dante devices' readings, and when the trim last changed (see virgil.autotrim).
    trim_readings: autotrim.ReadingHistory = field(default_factory=autotrim.ReadingHistory, init=False)
    _replays: Replays | None = field(default=None, init=False)
    # The latest release GitHub told us of, and when we last asked (see virgil.updates).
    update_check: dict = field(default_factory=dict, init=False)
    _update_wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    removed_notice: str | None = field(default=None, init=False)
    # Where evidence about cards' transmit timestamp slots is kept, and what was seen this session.
    timestamping_path: Path = field(default=timestamping.EVIDENCE_PATH, init=False)
    slots_watch: timestamping.Watch = field(default_factory=timestamping.Watch, init=False)
    slots_lost: str | None = field(default=None, init=False)
    _slots_checked_at: float = field(default_factory=time.time, init=False)
    _graphed: dict = field(default_factory=lambda: {"clock": None, "polls": None}, init=False)
    # Members' graphs, fetched for the page: member ID to (when, series).
    _member_graphs: dict = field(default_factory=dict, init=False)
    events_path: Path = field(default=EVENTS_PATH, init=False)
    # The interfaces whose DHCP lease names were last set, so they're only set again on a change.
    dhcp_names_for: tuple | None = field(default=None, init=False)
    # What auto-trim last did, for status.
    last_trim: dict | None = field(default=None, init=False)
    # (trim applied, consoles' average) for recent steps, and why auto-trim paused, if it has.
    trim_history: list = field(default_factory=list, init=False)
    trim_paused: str | None = field(default=None, init=False)
    # When auto-trim paused (monotonic, and wall time for status), and the consoles it was steering by.
    trim_paused_at: float = field(default=0.0, init=False)
    trim_paused_wall: float | None = field(default=None, init=False)
    trim_paused_consoles: frozenset = field(default_factory=frozenset, init=False)

    def __post_init__(self) -> None:
        self.config = config_file.load(self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime
        self._config_on_disk = config_file.to_dict(self.config)
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
                                 ntp_hwtimestamp=self.ntp_hwtimestamp_interface(),
                                 probe_timestamps=config.probe_timestamps, serve_ptpv2=self.wants_ptpv2(),
                                 tuning=Tuning(phc2sys_kp=config.phc2sys_kp, phc2sys_ki=config.phc2sys_ki,
                                             filter=config.chrony_filter))
        # chrony follows this address, and NTP isn't authenticated, so it must be one the leader has proven is
        # its own: where its heartbeats come from (see verified_address). An address from an mDNS advert won't
        # do: anyone on the management network could advertise the leader's Virgil ID with their own address,
        # and this Virgil's clock (and its Dante devices') would have followed them. A follower has always
        # heard its leader's heartbeat, so this is only ever missing for a moment after virgild starts, and
        # the timing manager tries again every second until it's there.
        leader = self.verified_address(target.leader_id) if target.leader_id else None
        if leader is None:
            return None
        return TimingRequest(Role.FOLLOWER, config.ptp_interface, leader, tuple(networks or [leader]),
                             config.trim_ppb, config.nic_profile is not None,
                             ntp_hwtimestamp=self.ntp_hwtimestamp_interface(),
                             probe_timestamps=config.probe_timestamps, serve_ptpv2=self.wants_ptpv2(),
                             tuning=Tuning(phc2sys_kp=config.phc2sys_kp, phc2sys_ki=config.phc2sys_ki,
                                             filter=config.chrony_filter))

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
        old_password, old_ptpv1 = self.config.web_password, self.config.ptpv1
        old_shared, old_check_updates = shared.snapshot(self.config), self.config.check_updates
        self.config = config_file.load(self.config_path)
        if self.config.check_updates and not old_check_updates:
            self.wake_update_check()  # turned back on with virgilctl: check now, as from the web UI
        for name, entry in shared.snapshot(self.config).items():
            if entry["changed"] > old_shared[name]["changed"] and entry["value"] != old_shared[name]["value"]:
                self.events.group("settings", f"This Virgil {shared.describe(name, entry['value'])} (with virgilctl)")
        if self.web is not None and self.config.web_password != old_password:
            self.web.sessions.end_all()
        if self.config.ptpv1 != old_ptpv1:
            try:
                self.change_ptpv1_mode(old_ptpv1, self.config.ptpv1)  # as from the web UI: make before break
            except RuntimeError:
                pass  # no event loop running (reloaded outside virgild's loop, as in some tests)
        self.config_mtime = mtime
        self._config_on_disk = config_file.to_dict(self.config)
        self._trust_members()
        log.info("Reloaded the config")
        return True

    def save(self) -> None:
        """Save the config, without losing anything virgilctl has changed in the file since virgild last read it.

        virgild holds the config in memory and writes the whole file; virgilctl edits the file, and virgild only
        looks for that every couple of seconds. Saving in between used to overwrite virgilctl's change (a new
        password, say, lost to an auto-trim step a second later). So if the file has changed, its changes are
        taken in first, exactly as a reload would, and only what virgild itself has changed since it last read or
        wrote the file is laid over them. If both changed the same setting, virgild's change, the later, wins.
        """
        try:
            changed = self.config_path.stat().st_mtime != self.config_mtime
        except OSError:
            changed = False  # no file to merge with: just write it
        if changed:
            before, ours = self._config_on_disk, config_file.to_dict(self.config)
            try:
                self.config = config_file.from_dict(before)  # so the reload sees only what virgilctl changed
                self.reload_if_changed()
                self.config = config_file.from_dict(_merge(before, ours, config_file.to_dict(self.config)))
            except (OSError, config_file.ConfigError) as error:
                log.warning("Couldn't take in the config file's changes before saving (%s); saving over them", error)
                self.config = config_file.from_dict(ours)
        config_file.save(self.config, self.config_path)
        self.config_mtime = self.config_path.stat().st_mtime
        self._config_on_disk = config_file.to_dict(self.config)

    # Heartbeats

    def heartbeat(self) -> Heartbeat:
        e = self.election
        devices = tuple(self.consoles.summaries()[:16]) if self.consoles else ()
        return Heartbeat(self.config.id, self.config.name, __version__, e.state, e.coin, e.leader,
                         e.lost_leader, e.handover_to, self.observing, e.established,
                         self.ptp.own_identity, tuple(self.ptp_warnings()[:8]), devices, self.clock,
                         tuple(self.alerts.open_summaries()[:16]) if self.alerts else (),
                         {"active": True, "size": self.recording.size()}
                         if self.recording is not None and self.recording.active else None,
                         self.serving(), self.local_device_settings(),
                         membership.digest(self.config.group) if self.config.group else None,
                         shared.snapshot(self.config) if self.config.group else None,
                         self.ptp_addresses[0] if self.config.ptp_interface and self.ptp_addresses else None)

    # Graphs

    def on_ping(self, address: str, rtt_ns: float | None) -> None:
        name = self.member_name_at(address)
        self._record_link(address, f"{self.config.name} ↔ {name} (Mgmt)", f"{self.config.name}/{name}", rtt_ns)

    def on_dante_ping(self, address: str, rtt_ns: float | None) -> None:
        """A ping across the Dante network, from the PTP interface to a member's, marked like Dante's audio."""
        name = next((p.heartbeat.name for p in self.peers.values() if p.heartbeat.ptp_address == address), address)
        self._record_link(("dante", address), f"{self.config.name} ↔ {name} (Dante)",
                          f"{self.config.name}/{name}/dante", rtt_ns)

    def _record_link(self, link, label: str, key: str, rtt_ns: float | None) -> None:
        now = time.time()
        # Each ping counts as 0% or 100% lost; averaged over a stretch, that's the loss rate.
        self.graphs.add(f"loss/{key}", "loss", label, "percent", now, 100.0 if rtt_ns is None else 0.0)
        if rtt_ns is None:
            return
        last, smoothed = self.link.get(link, (None, None))
        smoothed = jitter(smoothed, rtt_ns, last)
        self.link[link] = (rtt_ns, smoothed)
        self.graphs.add(f"rtt/{key}", "rtt", label, "ns", now, rtt_ns, WORST)
        self.graphs.add(f"jitter/{key}", "jitter", label, "ns", now, smoothed, WORST)

    @property
    def replays(self) -> Replays:
        """The log prepared for replay, kept beside the saved logs (see virgil.replay)."""
        if self._replays is None or self._replays.root != self.recordings_root:
            self._replays = Replays(self.recordings_root, self.recordings_root.parent / "replay")
        return self._replays

    async def check_for_updates(self) -> None:
        """Ask GitHub for the latest release, once (see virgil.updates)."""
        try:
            found = await updates.fetch_latest(__version__)
        except Exception as error:  # no internet access is normal for a Clock Box: try again tomorrow, quietly
            self.update_check = {**self.update_check, "checked": time.time(), "error": str(error) or type(error).__name__}
            log.info("Couldn't check for a newer Virgil: %s", self.update_check["error"])
            return
        self.update_check = {**found, "checked": time.time()}
        if found["available"]:
            log.info("Virgil %s is available (this is %s): %s", found["latest"], __version__, found["url"])

    def wake_update_check(self) -> None:
        """Check now, rather than waiting for tomorrow's check (when the setting is turned back on)."""
        self._update_wake.set()

    async def watch_updates(self, first: float = updates.FIRST_CHECK, every: float = updates.CHECK_EVERY) -> None:
        """Once a day, while check_updates is on: is there a newer Virgil? Nothing is ever installed."""
        wait = first
        while True:
            try:
                await asyncio.wait_for(self._update_wake.wait(), wait)
            except TimeoutError:
                pass
            self._update_wake.clear()
            wait = every
            if self.config.check_updates:
                await self.check_for_updates()

    def dante_pinger(self) -> Pinger | None:
        """A pinger for the Dante network, if this Virgil's PTP interface is separate from its management one."""
        interface = self.config.ptp_interface
        if not interface or interface == self.config.mgmt_interface:
            return None
        return Pinger(self.on_dante_ping, interface=interface, dscp=DANTE_AUDIO_DSCP)

    def dante_ping_targets(self) -> set[str]:
        """Each member's PTP address, when pinging across the Dante network tells us anything new: only when this
        Virgil's PTP interface isn't also its management interface (then the usual pings already cover it)."""
        if not self.config.ptp_interface or self.config.ptp_interface == self.config.mgmt_interface:
            return set()
        # As with the usual pings, every member with a known address, heard lately or not: a member gone quiet
        # should show up as lost pings.
        members = set(filter(self.pings_to, self.member_ids()))
        return {p.heartbeat.ptp_address for i, p in self.peers.items() if i in members and p.heartbeat.ptp_address}

    def member_name_at(self, address: str) -> str:
        for virgil_id in self.member_ids():
            if address in self.addresses_for(virgil_id):
                member = self.config.group.members.get(virgil_id)
                return member.name if member else address
        return address

    def pings_to(self, virgil_id: str) -> bool:
        """Whether this Virgil pings that member. A round trip is the same from either end, so only one of each pair
        pings (the one whose ID sorts first); every page merges the group's graphs, so both still show it."""
        return self.config.id < virgil_id

    def ping_targets(self) -> set[str]:
        """One management address for each member this Virgil pings (see pings_to)."""
        targets = set()
        for virgil_id in filter(self.pings_to, self.member_ids()):
            addresses = self.addresses_for(virgil_id)
            if addresses:
                targets.add(addresses[0])
        return targets

    def sample_graphs(self, now: float) -> None:
        """One second's worth of this Virgil's own figures. Members graph their own, and the page merges them."""
        name, state, clock = self.config.name, self.election.state, self.clock or {}
        add = self.graphs.add
        # Each reading is recorded once, when it's new, rather than repeated every second as a step.
        new_clock = self.clock is not None and self.clock is not self._graphed["clock"]
        self._graphed["clock"] = self.clock
        polls = getattr(self.consoles, "polls", None)
        new_consoles = polls is None or polls != self._graphed["polls"]
        self._graphed["polls"] = polls
        if new_clock and state in (State.FOLLOWER, State.SYNC):
            add(f"offset/{name}", "offset", name, "ns", now, clock.get("system_offset_ns"))
            add(f"phc/{name}", "phc", name, "ns", now, clock.get("phc_offset_ns"))
        elif new_clock and state is State.LEADER and clock.get("phc_trim_ppm") is not None:
            add(f"trim/{name}", "trim", name, "ppm", now, clock["phc_trim_ppm"])
        for device in self.consoles.summaries() if self.consoles and new_consoles else []:
            label, key = self._console_label(device.get("name"))
            offset = device.get("clock_offset_ppb")
            add(f"console/{key}", "console", label, "ppm", now, offset / 1000 if isinstance(offset, int) else None)
            add(f"limit/{key}", "latency", f"{label} setting", "ns", now, device.get("latency_setting_ns"))
            if not self._has_history.get(key):
                # No history from netaudio: fall back to the latest figures.
                add(f"latency/{key}", "latency", f"{label} worst flow", "ns", now, device.get("latency_ns"), WORST)
                add(f"average/{key}", "latency", f"{label} average", "ns", now, device.get("latency_average_ns"))
            late = device.get("late_packets")
            if isinstance(late, int):
                # A rate since the previous reading. (Since the count last changed, a burst after hours of none was
                # divided by hours: a spike labelled 0, totalling 0.)
                before = self.late_counts.get(key)
                self.late_counts[key] = (late, now)
                if before is not None and late >= before[0] and now > before[1]:
                    add(f"late/{key}", "late", label, "per_s", now, (late - before[0]) / (now - before[1]), WORST)
        self.graphs.forget_before(now - KEEP_SECONDS)

    def _console_label(self, device_name: str | None) -> tuple[str, str]:
        return f"{device_name} ({self.config.name})", f"{self.config.name}/{device_name}"

    def on_latency_history(self, device, points) -> None:
        """netaudio's own latency history for a console, at its own resolution and times."""
        label, key = self._console_label(device.name)
        self._has_history[key] = True
        for t, worst, average in points:  # already placed on the wall clock (see consoles.latency_history)
            self.graphs.add(f"latency/{key}", "latency", f"{label} worst flow", "ns", t, worst, WORST)
            self.graphs.add(f"average/{key}", "latency", f"{label} average", "ns", t, average)

    async def watch_graphs(self, pinger: Pinger | None = None, dante_pinger: Pinger | None = None) -> None:
        # Started with spawn (see virgil.tasks), like every background task here: kept until it finishes, and a
        # failure logged when it happens. These two run for as long as virgild does.
        if pinger is not None:
            spawn(pinger.run(), "Pinging the other members")
        if dante_pinger is not None:
            spawn(dante_pinger.run(), "Pinging across the Dante network")
        while True:
            await asyncio.sleep(1.0)
            try:
                if pinger is not None:
                    pinger.targets = self.ping_targets()
                if dante_pinger is not None:
                    dante_pinger.targets = self.dante_ping_targets()
                self.sample_graphs(time.time())
            except Exception:
                log.exception("Couldn't sample the graphs")

    @staticmethod
    def _window(request: web.Request) -> float:
        try:
            return min(max(float(request.query.get("window", 300)), 10), KEEP_SECONDS)
        except ValueError:
            raise web.HTTPBadRequest(text="window must be a number of seconds") from None

    async def time_api(self, request: web.Request) -> web.Response:
        """This Virgil's clock, for the page to compare with the device it's on."""
        return web.json_response({"now": time.time()})

    async def graphs_api(self, request: web.Request) -> web.Response:
        """This Virgil's graphs, and every member's, merged for the page."""
        window = self._window(request)
        series = self.graphs.window(window, time.time())
        results = await asyncio.gather(*(self._member_series(i, window) for i in self.member_ids()))
        for member_series in results:
            series += member_series
        return web.json_response({"window": window, "series": series})

    # Recording a whole group: the Virgil whose page was used coordinates; members follow its lead.

    async def _member_call(self, virgil_id: str, method: str, path: str, body: dict | None = None,
                           timeout: float = 10.0, as_bytes: bool = False):
        """A request to a member over the group connection. None if it doesn't answer."""
        addresses = self.addresses_for(virgil_id)
        if not addresses:
            return None
        advert = self.adverts.get(virgil_id)
        port = advert.group_port if advert else self.group_port
        context = tls.group_client_context(self.identity, self.member_certs())
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.request(method, f"https://{addresses[0]}:{port}{path}", json=body,
                                           ssl=context) as reply:
                    if reply.status != 200:
                        return None
                    return await reply.read() if as_bytes else await reply.json()
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return None

    def _heard_members(self) -> list[str]:
        now = time.monotonic()
        return [i for i in self.member_ids()
                if i in self.peers and now - self.peers[i].heard_at <= self.election.timing.visible]

    async def start_recording(self, group: bool = True) -> dict:
        stamp = stamp_now()
        name = await self.recording.start(stamp, group=group)
        self.recording_here = True
        self.recording_members = []
        if group:
            for virgil_id in self._heard_members():
                reply = await self._member_call(virgil_id, "POST", RECORDING_PATH, {"action": "start", "stamp": stamp})
                if reply is not None:
                    self.recording_members.append(virgil_id)
        names = [self.config.group.members[i].name for i in self.recording_members
                 if self.config.group and i in self.config.group.members]
        return {"name": name, "members": names}

    async def stop_recording(self) -> dict:
        """Stop here and on the members this Virgil started, and bundle everything into one file."""
        own = await self.recording.stop()
        self.recording_here = False
        if own is None:
            return {"archive": None}
        archives = [self.recordings_root / own]
        for virgil_id in self.recording_members:
            reply = await self._member_call(virgil_id, "POST", RECORDING_PATH, {"action": "stop"},
                                            timeout=LOG_PACK_TIMEOUT)
            name = reply.get("archive") if isinstance(reply, dict) else None
            if not name:
                continue
            # The name comes from the member, and becomes a file here, so it must be a plain archive name: the
            # same pattern this Virgil's own archives are held to. Unchecked, a name such as
            # "../../../etc/dante-virgil/virgil.toml" wrote the member's data over this Virgil's own files.
            if not isinstance(name, str) or not ARCHIVE.fullmatch(name):
                log.warning("Ignored a log from %s: %r isn't an archive's name", self.name_of(virgil_id), name)
                continue
            data = await self._member_call(virgil_id, "GET", f"{RECORDINGS_PATH}/{name}",
                                           timeout=LOG_SEND_TIMEOUT, as_bytes=True)
            if data is None:
                continue
            path = self.recordings_root / name
            path.write_bytes(data)
            archives.append(path)
            await self._member_call(virgil_id, "POST", f"{RECORDINGS_PATH}/delete", {"name": name})
        self.recording_members = []
        if len(archives) == 1:
            return {"archive": own}
        bundle = self.recordings_root / own.replace(".tar.gz", "").replace(
            f"-{self.config.name}", "").rstrip("-")
        bundle = bundle.with_name(bundle.name + "-group.tar")

        def pack() -> None:
            import io
            import tarfile

            from virgil import summary

            # The group's summary, from each Virgil's own: at the top of the bundle, and beside it.
            summaries = [s for s in map(summary_of, archives) if s]
            group = {"virgils": summaries}
            files = {"summary.json": json.dumps(group, indent=1).encode(),
                     "summary.html": summary.render(summaries).encode()}
            with tarfile.open(bundle, "w") as tar:
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
                    tar.addfile(info, io.BytesIO(data))
                for archive in archives:
                    tar.add(archive, arcname=archive.name)
            summary_path(bundle).write_bytes(files["summary.json"])
            for archive in archives:
                archive.unlink(missing_ok=True)
                summary_path(archive).unlink(missing_ok=True)

        await asyncio.to_thread(pack)
        return {"archive": bundle.name}

    # Consoles' enforce settings, kept by the Virgil each console is plugged into.

    def set_device(self, mac: str, setting: str, enabled: bool) -> dict:
        current = self.config.devices.get(mac, DeviceSettings())
        if setting == "ptpv2":
            current.native_ptpv2 = enabled
        else:
            current.aes67 = enabled
        self.config.devices[mac] = current
        self.save()
        log.info("%s enforcing %s for %s", "Now" if enabled else "No longer", setting, mac)
        return {"mac": mac, "native_ptpv2": current.native_ptpv2, "aes67": current.aes67}

    def local_device_settings(self) -> dict:
        """The enforce settings of this Virgil's own consoles, for heartbeats."""
        settings = {}
        for device in list(self.consoles.devices.values())[:16] if self.consoles else []:
            if device.mac:
                chosen = self.config.devices.get(device.mac, DeviceSettings())
                settings[device.mac] = {"native_ptpv2": chosen.native_ptpv2, "aes67": chosen.aes67}
        return settings

    async def group_device(self, request: web.Request) -> web.Response:
        """A member's page changed one of this Virgil's consoles' settings."""
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        mac = normalise_mac(body.get("mac"))
        setting, enabled = body.get("setting"), body.get("enabled")
        if mac is None or setting not in ("ptpv2", "aes67") or not isinstance(enabled, bool):
            raise web.HTTPBadRequest()
        log.info("%s changed a Dante device's setting from its page", self.config.group.members[sender].name)
        return web.json_response(self.set_device(mac, setting, enabled))

    # Setting the group's time: on the leader, its system clock and PHC; then every follower resynchronises.

    async def set_group_time(self, delta: float, systemd: roles.Systemd | None = None) -> dict:
        """Move the group's clock by delta seconds. Every console re-locks, so audio stops for a few seconds."""
        if self.timing is None:
            raise RuntimeError("virgild doesn't manage the timing services here (observe_only is set)")
        if self.election.state is not State.LEADER:
            leader = self.election.leader
            if leader is None:
                raise RuntimeError("there's no leader to set the time on")
            reply = await self._member_call(leader, "POST", SETTIME_PATH, {"delta": delta}, timeout=60)
            if reply is None:
                raise RuntimeError("the leader didn't answer")
            return reply
        atomic_write(SETTIME_REQUEST, json.dumps({"delta": delta}).encode(), mode=0o640)
        await asyncio.to_thread((systemd or roles.Systemd()).start, SETTIME_UNIT)
        log.warning("Moved the group's clock by %+.3f s; the Dante devices will re-lock", delta)
        self.events.group("time", f"The group's time was set: moved by {delta:+.3f} s")
        followers = []
        for virgil_id in self._heard_members():
            if await self._member_call(virgil_id, "POST", RESYNC_PATH, {}) is not None:
                followers.append(self.config.group.members[virgil_id].name)
        return {"moved_by": delta, "leader": self.config.name, "resynchronised": followers}

    async def group_settime(self, request: web.Request) -> web.Response:
        """A follower's page asked to set the time; the leader does it for the whole group."""
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        try:
            delta = float((await request.json())["delta"])
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest() from None
        try:
            return web.json_response(await self.set_group_time(delta))
        except (RuntimeError, OSError, roles.ServiceError) as error:
            return web.json_response({"error": str(error)}, status=409)

    async def group_resync(self, request: web.Request) -> web.Response:
        """The leader's time was set: set the timing services up from scratch, stepping to its new time."""
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        if self.timing is None:
            return web.json_response({"error": "not managing timing"}, status=409)
        self.events.group("time", f"{self.name_of(sender)} set the group's time, so this Virgil resynchronised")
        self.timing.resync()
        return web.json_response({"resynchronising": True})

    # The member list: kept in step across the group (see virgil.membership).

    @property
    def events(self) -> EventLog:
        if self._event_log is None:
            self._event_log = EventLog(self.events_path)
        return self._event_log

    def adopt_shared_settings(self, theirs: dict, sender: str) -> None:
        """Take on settings another member changed more recently (see virgil.shared)."""
        old_password, before = self.config.web_password, shared.snapshot(self.config)
        adopted = shared.adopt(self.config, theirs)
        if shared.snapshot(self.config) == before:
            return  # the usual case: nothing newer, and nothing to save
        self.save()
        for name, value in adopted:
            self.events.group("settings", f"{self.name_of(sender)} {shared.describe(name, value)}")
        if self.web is not None and self.config.web_password != old_password:
            self.web.sessions.end_all()

    def clock_settled(self) -> bool:
        """Whether this Virgil's clock can stamp changes its group shares (see virgil.shared's clock_settled)."""
        return shared.clock_settled(self.config.group is not None, self.election.state.value,
                                    bool((self.clock or {}).get("synchronised")))

    def shared_setting_changed_here(self, name: str, value, how: str) -> None:
        """A shared setting changed on this Virgil: stamp it, so it spreads, and record it. Only once its clock has
        settled (see clock_settled): callers check first."""
        stamp = f"{name}_changed"
        setattr(self.config, stamp, shared.next_stamp(getattr(self.config, stamp), time.time()))
        self.events.group("settings", f"This Virgil {shared.describe(name, value)} ({how})")

    def apply_member_list(self, data, source: str) -> list[tuple[str, str]]:
        """Merge another member's list into ours. Returns what changed."""
        group = self.config.group
        if group is None:
            return []
        try:
            members, removed = membership.parse(data, group.id)
        except GroupError as error:
            log.warning("Ignored a member list from %s: %s", source, error)
            return []
        changes = membership.merge(group, members, removed)
        if changes:
            self._member_list_changed(changes, source)
        return changes

    def _member_list_changed(self, changes: list[tuple[str, str]], source: str) -> None:
        self.save()
        self._trust_members()
        for kind, words in changes:
            if kind == "changed":
                log.info("Group: %s (learned from %s)", words, source)
            else:
                self.events.group(kind, words)
        for virgil_id in list(self.peers):
            if virgil_id not in self.config.group.members:
                del self.peers[virgil_id]  # a removed leader is lost at once, and the rest re-elect
        if self.config.id not in self.config.group.members:
            log.warning("This Virgil was removed from the group, so it's leaving it")
            self.events.group("removed", "This Virgil was removed from the group, and carries on alone")
            self._leave_locally()

    def _leave_locally(self) -> None:
        self.config.group = None
        self.save()
        self.peers.clear()
        self.removed_notice = None
        self.group_changed()

    async def sync_members_from(self, virgil_id: str) -> None:
        data = await self._member_call(virgil_id, "GET", MEMBERS_PATH, timeout=5)
        if data is not None:
            self.apply_member_list(data, self.name_of(virgil_id))

    async def push_members(self, to: list[str] | None = None, group=None) -> list[str]:
        """Tell members about our list straight away. Returns the names of those that took it."""
        group = group or self.config.group
        if group is None:
            return []
        targets = to if to is not None else self.member_ids()
        data = membership.snapshot(group)
        replies = await asyncio.gather(*(self._member_call(i, "POST", MEMBERS_PATH, data, timeout=5) for i in targets))
        return [self.name_of(i) for i, reply in zip(targets, replies) if reply is not None]

    async def remove_member(self, virgil_id: str) -> str:
        """Remove another member: tell it (while it still trusts us), then everyone else."""
        group = self.config.group
        if group is None:
            raise RuntimeError("this Virgil isn't in a group")
        if virgil_id == self.config.id:
            raise RuntimeError("a Virgil can't remove itself; it can leave the group instead")
        if virgil_id not in group.members:
            raise RuntimeError("that Virgil isn't a member of this group")
        if not self.clock_settled():
            raise RuntimeError(shared.UNSETTLED)
        name = group.members[virgil_id].name
        updated = copy.deepcopy(group)
        stamp = shared.next_stamp(group.members[virgil_id].changed, time.time())  # later than the joining it undoes
        membership.remove(updated, virgil_id, stamp, by=self.config.id)
        told = await self.push_members([virgil_id], updated)  # so it leaves, if it can be reached
        self._member_list_changed(membership.merge(group, updated.members, updated.removed), "this Virgil")
        others = await self.push_members()
        log.info("Removed %s from the group (%s told it; %s told)", name, "it was" if told else "nobody",
                 ", ".join(others) or "no other member")
        return name

    async def leave_group(self) -> None:
        """Leave the group: a removal of ourselves, told to everyone, then carrying on alone."""
        group = self.config.group
        if group is None:
            raise RuntimeError("this Virgil isn't in a group")
        updated = copy.deepcopy(group)
        # Leaving is always allowed, even before this Virgil's clock has settled (a Virgil with a broken clock is
        # just the one that might want out), but then it's stamped just after the latest change this Virgil knows
        # of, rather than by its own clock, so it can't claim to be from the future either.
        joined = group.members[self.config.id].changed if self.config.id in group.members else 0.0
        if self.clock_settled():
            stamp = shared.next_stamp(joined, time.time())
        else:
            stamp = max(membership.latest(group), joined) + 0.001
        membership.remove(updated, self.config.id, stamp, by=self.config.id)
        told = await self.push_members(group=updated)
        log.info("Left the group (told %s)", ", ".join(told) or "no member")
        self.events.group("left", "This Virgil left the group, and carries on alone")
        self._leave_locally()

    async def group_members_get(self, request: web.Request) -> web.Response:
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        return web.json_response(membership.snapshot(self.config.group))

    async def group_members_post(self, request: web.Request) -> web.Response:
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        return web.json_response({"changes": self.apply_member_list(data, self.name_of(sender))})

    async def membership_api(self, request: web.Request) -> web.Response:
        """Public: whether a Virgil is a member, or was removed. A removed Virgil that was offline asks this."""
        virgil_id = request.query.get("id", "")
        group = self.config.group
        if group is None or not HEX_ID.match(virgil_id):
            return web.json_response({"member": False, "removed": False})
        removed = group.removed.get(virgil_id)
        return web.json_response({"member": virgil_id in group.members, "removed": removed is not None,
                                  "changed": removed.changed if removed else None})

    async def check_still_member(self) -> None:
        """Hearing no member for a while: ask them, on their public ports, whether this Virgil was removed.

        Each answer comes over TLS checked against that member's pinned certificate, so nobody else can fake it.
        """
        group = self.config.group
        if group is None or not self.member_ids():
            self.removed_notice = None
            return
        now = time.monotonic()
        if any(now - p.heard_at < REMOVED_CHECK_AFTER for p in self.peers.values()):
            self.removed_notice = None
            return
        own = group.members.get(self.config.id)
        for virgil_id in self.member_ids():
            addresses = self.addresses_for(virgil_id)
            advert = self.adverts.get(virgil_id)
            if not addresses:
                continue
            context = ssl.create_default_context(cadata=group.members[virgil_id].cert)
            context.check_hostname = False
            url = f"https://{addresses[0]}:{advert.public_port if advert else PUBLIC_PORT}{MEMBERSHIP_PATH}?id={self.config.id}"
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                    async with session.get(url, ssl=context) as reply:
                        answer = await reply.json()
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                continue
            if answer.get("removed") and (own is None or (answer.get("changed") or 0) >= own.changed):
                self.removed_notice = f"{group.members[virgil_id].name} says this Virgil was removed from the group"
                return
        self.removed_notice = None

    async def watch_membership(self) -> None:
        while True:
            await asyncio.sleep(30.0)
            try:
                await self.check_still_member()
            except Exception:
                log.exception("Couldn't check this Virgil's membership")

    async def control_remove(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            target = str(body.get("member", ""))
        except (ValueError, AttributeError):
            return web.json_response({"error": "say which member to remove"}, status=400)
        group = self.config.group
        matches = [i for i, m in (group.members.items() if group else []) if target in (i, m.name)]
        if len(matches) != 1:
            return web.json_response({"error": f"no single member called {target}"}, status=404)
        try:
            return web.json_response({"removed": await self.remove_member(matches[0])})
        except RuntimeError as error:
            return web.json_response({"error": str(error)}, status=409)

    async def control_leave(self, request: web.Request) -> web.Response:
        try:
            await self.leave_group()
        except RuntimeError as error:
            return web.json_response({"error": str(error)}, status=409)
        return web.json_response({"left": True})

    async def group_recording(self, request: web.Request) -> web.Response:
        """A member, coordinating a group recording, starts or stops this Virgil's part of it."""
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        if body.get("action") == "start":
            if self.recording.active:
                return web.json_response({"error": "already recording"}, status=409)
            return web.json_response({"name": await self.recording.start(body.get("stamp"), group=True)})
        if body.get("action") == "stop":
            # If this Virgil's part already stopped itself (its disk filled, say), its log is still here to collect.
            archive = await self.recording.stop() or self.recording.take_auto_stopped()
            return web.json_response({"archive": archive})
        raise web.HTTPBadRequest()

    async def group_recording_file(self, request: web.Request) -> web.StreamResponse:
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        try:
            path = self.recording.path(request.match_info["name"])
            size = path.stat().st_size
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        # Paced, since it crosses the radio link: a log sent flat out could crowd out the audio.
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})
        await response.prepare(request)
        started, sent = time.monotonic(), 0
        with path.open("rb") as f:
            while chunk := f.read(LOG_CHUNK):
                await response.write(chunk)
                sent += len(chunk)
                ahead = sent / LOG_RATE - (time.monotonic() - started)
                if ahead > 0:
                    await asyncio.sleep(ahead)
        await response.write_eof()
        return response

    async def group_recording_delete(self, request: web.Request) -> web.Response:
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        try:
            self.recording.delete((await request.json()).get("name", ""))
        except (FileNotFoundError, ValueError, AttributeError):
            raise web.HTTPNotFound() from None
        return web.json_response({"deleted": True})

    async def group_graphs(self, request: web.Request) -> web.Response:
        """This Virgil's own graphs, for a member's page."""
        sender = self._member_for_certificate(request)
        if sender is None or sender == self.config.id:
            raise web.HTTPForbidden()
        return web.json_response({"series": self.graphs.window(self._window(request), time.time())})

    async def _member_series(self, virgil_id: str, window: float) -> list:
        """A member's own graphs, at most a second or so old. Members that don't answer are left out."""
        cached = self._member_graphs.get(virgil_id)
        if cached and time.monotonic() - cached[0] < 1.5 and cached[1] == window:
            return cached[2]
        addresses = self.addresses_for(virgil_id)
        peer = self.peers.get(virgil_id)
        if not addresses or peer is None or time.monotonic() - peer.heard_at > self.election.timing.visible:
            return []
        advert = self.adverts.get(virgil_id)
        port = advert.group_port if advert else self.group_port
        url = f"https://{addresses[0]}:{port}{GRAPHS_PATH}?window={window}"
        context = tls.group_client_context(self.identity, self.member_certs())
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
                async with session.get(url, ssl=context) as reply:
                    if reply.status != 200:
                        return []
                    series = (await reply.json()).get("series") or []
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return []
        self._member_graphs[virgil_id] = (time.monotonic(), window, series)
        return series

    # PTPv1

    def serving_ptpv2(self, now: float) -> bool:
        """Whether ptp4l is serving: its Announces have gone out lately."""
        return self.ptp.own_announced_at is not None and now - self.ptp.own_announced_at <= 10.0

    def _device_names(self, addresses) -> str:
        names = {d.address: d.name for d in self.consoles.devices.values()} if self.consoles else {}
        return " and ".join(names.get(a, a) for a in addresses)

    def wants_ptpv2(self) -> bool:
        """Whether ptp4l should be serving PTPv2: always, except in the "only" PTPv1 mode."""
        if self.serve_ptpv2_wanted is None:
            self.serve_ptpv2_wanted = self.config.ptpv1 != "only"
        return self.serve_ptpv2_wanted

    def ptp_mac(self) -> str | None:
        """The PTP interface's MAC: the grandmaster identity PTPv1 announces. ptp4l's, if it's serving."""
        own = mac_of_identity(self.ptp.own_identity)
        if own is not None:
            return own
        try:
            return Path(f"/sys/class/net/{self.config.ptp_interface}/address").read_text().strip().lower() or None
        except (OSError, TypeError):
            return None

    def ptp4l_running(self) -> bool:
        """Whether ptp4l is (or is about to be) running: from what virgild asked for, not from its last Announce,
        which lingers for a few seconds after it stops."""
        if self.wants_ptpv2():
            return True
        if self.timing is not None:
            return self.timing.request is not None and self.timing.request.serve_ptpv2
        return self.serving_ptpv2(time.monotonic())  # not managing the timing services: go by what's heard

    def ptpv1_hardware_tx(self) -> bool:
        """Whether PTPv1 should use the card's transmit timestamps: when it has them to itself, or can share them."""
        slots = self.ptp_slots()
        if self.ptp4l_running():
            return slots.can_share
        return slots.count != 0  # ptp4l isn't using them: any card with hardware transmit timestamps will do

    def ptpv1_wanted(self, now: float) -> tuple[bool, str]:
        """Whether this Virgil should serve PTPv1, and why, following the ptpv1 setting."""
        mode = self.config.ptpv1
        if mode == "off":
            return False, "off in this Virgil's settings"
        if self.config.ptp_interface is None or self.ptp_watcher is None:
            return False, "there's no PTP interface to serve on"
        if not self.ptp_addresses:
            return False, f"{self.config.ptp_interface} has no IPv4 address"
        if mode == "only":
            if self.ptp_mac() is None:
                return False, f"couldn't read {self.config.ptp_interface}'s MAC address"
            return True, "PTPv1 only, instead of PTPv2"
        if not self.serving_ptpv2(now) or mac_of_identity(self.ptp.own_identity) is None:
            return False, "this Virgil isn't serving PTPv2, so there's no clock to serve"
        if mode == "always":
            return True, "always on in this Virgil's settings"
        if now - self.ptp_watch_started < 20.0:
            return False, "watching the network before deciding"
        bridging = self.ptp.bridging(now)
        if bridging:
            return False, f"{self._device_names(bridging)} is bridging this Virgil's PTPv2 into PTPv1"
        capable = [d for d in self.consoles.devices.values() if d.ptpv2 or d.aes67] if self.consoles else []
        if capable:
            names = " and ".join(d.name for d in capable)
            return False, f"{names} can bridge this Virgil's PTPv2 into PTPv1"
        serving = self.ptpv1_server is not None and self.ptpv1_server.running
        if not serving and not self.ptp.ptpv1_heard(now):
            return False, "no PTPv1 devices here"
        return True, "no device here bridges this Virgil's PTPv2 into PTPv1"

    def ptp_slots(self) -> timestamping.Slots:
        """What Virgil believes about the PTP interface's transmit timestamp slots."""
        interface = self.config.ptp_interface
        found = get_interface(interface, timestamping=lambda name: None) if interface else None
        return timestamping.slots_for(interface or "", found.driver if found else None, path=self.timestamping_path)

    async def _command(self, *args: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(process.communicate(), 10)
            return out.decode(errors="replace")
        except (OSError, TimeoutError):
            return ""

    async def check_shared_timestamps(self) -> None:
        """While ptp4l and PTPv1 share the card's transmit timestamps, watch for any going missing.

        A rising skipped-timestamp counter, or ptp4l timing out, means the card can't really share: PTPv1
        moves to the kernel's transmit timestamps straight away, and the card is treated as having one slot
        from then on, even after a reboot.
        """
        server = self.ptpv1_server
        since, self._slots_checked_at = self._slots_checked_at, time.time()
        if server is None or not server.running or not server.hardware_tx:
            self.slots_watch.baseline = None  # a fresh baseline when sharing starts again
            return
        interface = self.config.ptp_interface
        reason = self.slots_watch.counters_rose(
            timestamping.skipped_counters(await self._command("ethtool", "-S", interface)))
        reason = reason or self.slots_watch.ptp4l_timed_out(await self._command(
            "journalctl", "-u", "dante-virgil-ptp4l", "--since", f"@{int(since)}", "-o", "cat", "--no-pager"))
        if reason is None:
            return
        found = get_interface(interface, timestamping=lambda name: None)
        timestamping.save_evidence(timestamping.card_key(interface, found.driver if found else None), reason,
                                   self.timestamping_path)
        server.set_hardware_tx(False)
        self.slots_lost = (f"{interface} lost a transmit timestamp while ptp4l and PTPv1 shared it ({reason}), "
                           "so PTPv1 now takes its transmit timestamps from the kernel")
        log.warning("%s", self.slots_lost)

    async def check_ptpv1(self, now: float | None = None, settle: float = PTPV1_SETTLE) -> None:
        """Start or stop serving PTPv1, once what's wanted has held for `settle` seconds.

        Together with ptpwatch.BRIDGING_SILENCE, a bridging console that goes quiet (while it reboots,
        say) is covered after about 30 seconds, and one that's back sooner than that never notices.
        """
        now = time.monotonic() if now is None else now
        wanted, reason = self.ptpv1_wanted(now)
        serving = self.ptpv1_server is not None and self.ptpv1_server.running
        self.ptpv1_reason = reason
        if wanted == serving:
            self.ptpv1_pending = None
            return
        # Every switch makes devices choose their leader again, so a change has to hold first.
        if self.ptpv1_pending is None or self.ptpv1_pending[0] != wanted:
            self.ptpv1_pending = (wanted, now)
        if now - self.ptpv1_pending[1] < settle:
            return
        self.ptpv1_pending = None
        if wanted:
            server = ptpv1_module.Server(self.config.ptp_interface, self.ptp_addresses[0], self.ptp_mac(),
                                         hardware_tx=self.ptpv1_hardware_tx())
            try:
                server.start()
            except OSError as error:
                log.warning("Couldn't serve PTPv1: %s", error)
                self.ptpv1_reason = f"couldn't open PTPv1's ports: {error}"
                return
            self.ptpv1_server = server
            self.events.group("ptpv1", f"Started serving PTPv1: {reason}")
        else:
            await self.ptpv1_server.stop()
            self.ptpv1_server = None
            self.events.group("ptpv1", f"Stopped serving PTPv1: {reason}")

    def change_ptpv1_mode(self, old: str, new: str) -> None:
        """React to a new ptpv1 setting. Into or out of "only", PTPv1 and ptp4l swap over in the background."""
        self.ptpv1_pending = None
        if (old == "only") == (new == "only"):
            spawn(self.check_ptpv1(settle=0), "Checking PTPv1 after its setting changed")
            return
        if self.ptpv1_switching is not None and not self.ptpv1_switching.done():
            self.ptpv1_switching.cancel()
        self.ptpv1_switching = spawn(self.switch_ptpv1_mode(new == "only"), "Switching between PTPv2 and PTPv1")

    async def _wait_for(self, done, timeout: float, step: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while not done():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(step)
        return True

    async def switch_ptpv1_mode(self, only: bool, settle: float = 3.0, follow_wait: float = 10.0) -> None:
        """Swap between PTPv2 (with PTPv1 as the setting says) and PTPv1 only, without a gap.

        Both serve the same PHC, so the time doesn't jump: the new protocol starts, the Dante devices move over
        to it, and only then does the old one stop. Nothing is set up from scratch.
        """
        if only:
            await self.check_ptpv1(settle=0)  # PTPv1 first, alongside ptp4l
            if self.ptpv1_server is not None and self.ptp.ptpv1_heard(time.monotonic()):
                await self._wait_for(lambda: bool(self.ptpv1_server.current_followers()), follow_wait)
            await asyncio.sleep(settle)
            self.serve_ptpv2_wanted = False
            if self.timing is not None:
                self.timing.poke()
                await self._wait_for(lambda: self.timing.request is not None
                                     and not self.timing.request.serve_ptpv2, 60.0)
            if self.ptpv1_server is not None:
                self.ptpv1_server.set_hardware_tx(self.ptpv1_hardware_tx())  # the slots are PTPv1's now
            self.events.group("ptpv1", "Switched to serving PTPv1 instead of PTPv2: ptp4l has stopped")
        else:
            self.serve_ptpv2_wanted = True
            if self.timing is not None:
                self.timing.poke()
            await self._wait_for(lambda: self.serving_ptpv2(time.monotonic()), 60.0)
            await asyncio.sleep(settle)  # time for the devices to hear ptp4l before PTPv1 might step back
            if self.ptpv1_server is not None:
                self.ptpv1_server.set_hardware_tx(self.ptpv1_hardware_tx())  # sharing again: only if the card can
            await self.check_ptpv1(settle=0)
            self.events.group("ptpv1", "Switched back to serving PTPv2")

    async def watch_ptpv1(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(5.0)
            tick += 1
            try:
                await self.check_ptpv1()
                if tick % 6 == 0:  # every 30 seconds
                    await self.check_shared_timestamps()
            except Exception:
                log.exception("Couldn't decide about PTPv1")

    def serving(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        server = self.ptpv1_server if self.ptpv1_server is not None and self.ptpv1_server.running else None
        return {"v2": self.serving_ptpv2(now), "v1": server is not None, "v1_reason": self.ptpv1_reason[:120]}

    async def watch_clock(self) -> None:
        while True:
            try:
                follower = self.election.state in (State.FOLLOWER, State.SYNC)
                self.clock = await clocks.read_clock(follower, None if follower else self.config.trim_ppb) or None
            except Exception:
                log.exception("Couldn't read the clocks")
            await asyncio.sleep(2.0)

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

        client = client or NetaudioClient()
        if hasattr(client, "issues"):
            def local_servers() -> set[str]:
                return {d.server_name for d in self.consoles.devices.values()} if self.consoles else set()

            def console_healthy(server_name: str) -> bool:
                """The console's own word: following this Virgil, with its servo synchronised."""
                for device in self.consoles.devices.values() if self.consoles else []:
                    if device.server_name == server_name:
                        return bool(device.follows_this_virgil) and device.servo in (None, "synchronized") \
                            and not (device.ptpv2 is False and device.aes67 is not True)
                return False

            self.alerts = Alerts(client.issues, local_servers, events_path=self.events_path,
                                 console_healthy=console_healthy, event_log=self.events)
        self.consoles = Consoles(client, local, lambda: self.config.devices,
                                 on_latency_history=self.on_latency_history,
                                 serving_ptpv2=self.wants_ptpv2,
                                 clock_identity=clock_identity, own_identity=lambda: self.ptp.own_identity,
                                 member_identities=member_identities)

    async def check_ptp_interface(self) -> None:
        """Follow the PTP interface's addresses, and keep netaudio's daemon on the right interface.

        netaudio needs an address. It runs on the PTP interface, where the
        consoles are, whenever that has one; otherwise on the management
        interface, so its UI still works for looking around the network.
        """
        interface = self.config.ptp_interface
        if interface is None:
            return
        # Dante devices may well be link-local, so every address counts here.
        self.ptp_addresses = tuple(ipv4_addresses(interface, link_local=True))
        if self.systemd is None:
            return
        wanted: tuple | None = None
        if self.ptp_addresses:
            wanted = (interface, self.ptp_addresses)
        else:
            mgmt = self.config.mgmt_interface
            mgmt_addresses = tuple(ipv4_addresses(mgmt, link_local=True)) if mgmt and mgmt != interface else ()
            if mgmt_addresses:
                wanted = (mgmt, mgmt_addresses)
        if wanted == self.netaudio_for:
            return
        # systemctl waits for the unit to stop, which can take as long as systemd allows a stuck service (90
        # seconds). Run here, on the event loop, that held up everything virgild does: no heartbeats went out
        # (members count a leader as lost after 30 seconds without them), and PTPv1's Syncs stopped. So it runs
        # in a thread, as the timing manager's own systemctl calls do, and only this check waits for it.
        try:
            if wanted is None:
                log.info("Neither %s nor the management interface has an IPv4 address, so netaudio's daemon is "
                         "stopped until one does", interface)
                await asyncio.to_thread(self.systemd.stop, NETAUDIO_UNIT)
            else:
                NETAUDIO_ENV.write_text(f"NETAUDIO_INTERFACE={wanted[0]}\n")
                await asyncio.to_thread(self.systemd.restart, NETAUDIO_UNIT)
                if wanted[0] == interface:
                    log.info("Started netaudio's daemon on %s", interface)
                else:
                    log.info("%s has no IPv4 address, so netaudio's daemon is on %s until it does",
                             interface, wanted[0])
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

    @property
    def netaudio_interface(self) -> str | None:
        """The interface netaudio's daemon is running on, if Virgil runs it."""
        if self.systemd is None or self.netaudio_for in (None, ("unknown",)):
            return None
        return self.netaudio_for[0]

    async def watch_alerts(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            if self.alerts is not None and (self.systemd is None or self.netaudio_running):
                try:
                    await self.alerts.poll()
                except Exception:
                    log.exception("Couldn't check for alerts")

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

    def ptp_timestamping(self, now: float) -> Timestamping | None:
        """The PTP interface's timestamping, checked at most once a minute (it takes a system call)."""
        interface = self.config.ptp_interface
        cached = self._timestamping_seen
        if cached is None or cached[0] != interface or now - cached[1] >= 60.0:
            found = get_interface(interface) if interface else None
            self._timestamping_seen = (interface, now, found.mode if found and found.physical else None)
        return self._timestamping_seen[2]

    def ptp_warnings(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        members = {}
        for peer in self.peers.values():
            if peer.heartbeat.ptp_identity:
                members[peer.heartbeat.ptp_identity] = peer.heartbeat.name
        explained = self.consoles.announcing_themselves() if self.consoles else set()
        warnings = self.ptp.leaks(members, explained)
        interface = self.config.ptp_interface
        if interface and self.ptp_timestamping(now) is Timestamping.SOFTWARE:
            # Lasting, not just when it's chosen: heartbeats carry it, so every page in the group shows it.
            warnings.append(f"{interface} only has software timestamping, which isn't stable enough for live use. "
                            "Use a NIC with hardware timestamping")
        if interface and self.ptp_watcher is not None and self._ptp_link_up():
            if not self.ptp_addresses:
                warnings.append(f"{interface} has no IPv4 address, so ptp4l can't send PTP to the Dante devices. "
                                "Check DHCP on the Dante devices' network")
            else:
                quiet = now - (self.ptp.last_heard if self.ptp.last_heard is not None else self.ptp_watch_started)
                if quiet >= PTP_QUIET_AFTER:
                    warnings.append(f"No PTP has been heard on {interface} for {quiet:.0f} s")
        if self.slots_lost:
            warnings.append(self.slots_lost)
        server = self.ptpv1_server
        if server is not None and server.running and server.timestamps() == {"send": "software", "receive": "software"}:
            warnings.append(f"PTPv1 on {interface} is using software timestamps only, so Dante devices following "
                            "it will be noticeably less steady")
        return [w[:200] for w in warnings]

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
        self.ptp_watch_started = time.monotonic()

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
        # Always, not only for members without an advert: this address is proven, and an advert's isn't.
        if request.remote and self.learned.get(sender) != request.remote:
            self.learned[sender] = request.remote
            if self.timing is not None and sender == self.election.leader:
                self.timing.poke()  # the leader has moved: point chrony at its new address
        if beat.shared_settings:
            self.adopt_shared_settings(shared.parse(beat.shared_settings), sender)
        if beat.members_digest and self.config.group and beat.members_digest != membership.digest(self.config.group):
            last = self._members_fetched.get(sender, 0.0)
            if time.monotonic() - last >= MEMBERS_FETCH_EVERY:
                self._members_fetched[sender] = time.monotonic()
                spawn(self.sync_members_from(sender), f"Fetching the member list from {self.name_of(sender)}")
        return web.Response(status=204)

    def verified_address(self, virgil_id: str) -> str | None:
        """The address a member was last heard from over the group's TLS, where only it holds the key."""
        return self.learned.get(virgil_id)

    def addresses_for(self, virgil_id: str) -> list[str]:
        """Addresses to try for a member: the proven one first (see verified_address), then any advertised.

        Advertised addresses are still worth trying, since that's how members find each other to begin with,
        and whatever answers there still has to present the member's certificate. But they never come before
        an address the member has proven, so a forged advert can't draw this Virgil's requests away from it.
        """
        addresses = []
        learned = self.learned.get(virgil_id)
        if learned:
            addresses.append(learned)
        advert = self.adverts.get(virgil_id)
        for address in advert.addresses if advert is not None else ():
            if address not in addresses:
                addresses.append(address)
        return addresses

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
        return list(self.console_readings(now).values())

    def console_readings(self, now: float) -> dict[str, int]:
        """Each following console's clock offset, by Virgil and device name."""
        sources = [(self.config.name, self.consoles.summaries() if self.consoles else [])]
        for virgil_id in self.member_ids():
            peer = self.peers.get(virgil_id)
            if peer is not None and now - peer.heard_at <= self.election.timing.visible:
                sources.append((peer.heartbeat.name, peer.heartbeat.dante_devices))
        return {
            f"{virgil}/{d.get('name')}": d["clock_offset_ppb"]
            for virgil, devices in sources for d in devices
            if d.get("follows_this_virgil") and isinstance(d.get("clock_offset_ppb"), int)
            and abs(d["clock_offset_ppb"]) <= autotrim.PLAUSIBLE
        }

    def resume_trim(self, reason: str) -> None:
        log.info("Auto-trim resuming: %s", reason)
        self.trim_paused = None
        self.trim_paused_wall = None
        self.trim_history.clear()

    def trim_once(self, now: float) -> None:
        """One auto-trim step, if this Virgil is leading and has consoles to go on."""
        if not self.config.auto_trim:
            self.trim_paused = None  # Turning it off and on again resumes it.
            self.trim_paused_wall = None
            self.trim_history.clear()
            return
        if self.timing is None or self.election.state is not State.LEADER:
            return
        if self.trim_paused:
            # A pause isn't for ever: it ends once the evidence for it is out of date.
            if now - self.trim_paused_at >= autotrim.RESUME_AFTER:
                self.resume_trim(f"it has been {autotrim.RESUME_AFTER / 60:.0f} minutes")
            elif frozenset(self.console_readings(now)) != self.trim_paused_consoles:
                self.resume_trim("the Dante devices it steers by have changed")
            else:
                return
        if now - self.election.since < autotrim.INTERVAL:
            self.trim_history.clear()
            return  # Let things settle after taking the lead.
        request = self.timing.request
        if request is None or request.trim_ppb != self.config.trim_ppb:
            return  # The last trim hasn't been applied yet.
        followers = [p.heartbeat.clock for i, p in self.peers.items() if i in self.member_ids()
                     and now - p.heard_at <= self.election.timing.visible and p.heartbeat.state is State.FOLLOWER]
        if not autotrim.followers_settled(followers):
            return  # A follower is still catching up; don't move the target again yet.
        # Each device's average over the last five minutes, not its reading right now (see virgil.autotrim).
        averages = self.trim_readings.averages(now, self.console_readings(now), self.config.trim_ppb)
        offsets = [round(v) for v in averages.values()]
        if not offsets:
            return  # not five minutes of readings from every following device yet
        average = sum(offsets) / len(offsets)
        # Whether the devices respond to the trim at all is judged by what they actually read: correcting for
        # the trim's changes would make them look as if they did.
        read = self.trim_readings.averages(now, self.console_readings(now), self.config.trim_ppb, corrected=False)
        self.trim_history = (self.trim_history + [(self.config.trim_ppb, sum(read.values()) / len(read))])[
            -autotrim.HISTORY:]
        if autotrim.unresponsive(self.trim_history):
            first_trim, first_average = self.trim_history[0]
            self.trim_paused = (
                f"the trim moved {(self.config.trim_ppb - first_trim) / 1000:+.3f} ppm but the Dante devices only "
                f"{(self.trim_history[-1][1] - first_average) / 1000:+.3f} ppm"
            )
            self.trim_paused_at = now
            self.trim_paused_wall = time.time()
            self.trim_paused_consoles = frozenset(self.console_readings(now))
            log.warning("Auto-trim paused: %s. It resumes in %.0f minutes, or as soon as the Dante devices change",
                        self.trim_paused, autotrim.RESUME_AFTER / 60)
            return
        new = autotrim.next_trim(self.config.trim_ppb, offsets)
        if new is None:
            return
        old = self.config.trim_ppb
        log.info("Auto-trim: Dante devices average %+.3f ppm, so the trim goes from %+.3f to %+.3f ppm",
                 average / 1000, old / 1000, new / 1000)
        self.config.trim_ppb = new
        try:
            self.save()
        except OSError as error:
            log.warning("Couldn't save the new trim: %s", error)
        self.timing.poke()
        self.trim_readings.trim_changed(now)
        self.last_trim = {"at": time.time(), "from_ppb": old, "to_ppb": new, "consoles_ppb": round(average)}

    async def watch_trim(self) -> None:
        ticks = round(autotrim.INTERVAL / autotrim.SAMPLE_EVERY)
        tick = 0
        while True:
            await asyncio.sleep(autotrim.SAMPLE_EVERY)
            tick += 1
            try:
                now = time.monotonic()
                self.trim_readings.add(now, self.console_readings(now), self.config.trim_ppb)
                if tick % ticks == 0:
                    self.trim_once(now)
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
        trim = max(-MAX_TRIM_PPB, min(MAX_TRIM_PPB, round(adjustment * 1000)))  # never more than a request allows
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
        # A new leader is worth a place in the history: whoever it is, as this Virgil sees it.
        leader_of = lambda state, leader: self.config.id if state is State.LEADER else leader
        old, new = leader_of(change.old_state, change.old_leader), leader_of(change.new_state, change.new_leader)
        if new is not None and new != old and not self.observing:
            self.events.group("leader", "This Virgil became the leader" if new == self.config.id
                              else f"{self.name_of(new)} became the leader")

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
            await self.bind_netaudio_ui()
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
                await self.check_ptp_interface()
            except Exception:
                log.exception("Couldn't check the network interfaces")
            await asyncio.sleep(NETWORK_CHECK_INTERVAL)

    # Public port

    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status_data())

    def status_data(self) -> dict:
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
                    "alerts": list(beat.alerts) if beat else [],
                    "recording": beat.recording if beat else None,
                    "serving": beat.serving if beat else None,
                    "device_settings": beat.device_settings if beat else {},
                }
            )
        others = [
            {"id": a.id, "name": a.name, "group_id": a.group_id, "addresses": list(a.addresses)}
            for a in self.adverts.values()
            if a.id not in self.member_ids()
        ]
        return (
            {
                "product": "Virgil",
                "version": __version__,
                "id": self.config.id,
                # This Virgil's clock, so the page can judge Virgil's timestamps by it rather than by its own device's.
                "now": time.time(),
                "name": self.config.name,
                "addresses": list(self.addresses),
                "observing": self.observing,
                "waiting_for_network": not self.electing,
                "state": self.election.state.value,
                "leader": self.name_of(self.election.leader),
                "group": None if group is None else {"id": group.id, "members": members,
                                                     "removed_notice": self.removed_notice,
                                                     "invites": outstanding_invites(group, time.time())},
                "clock": self.clock,
                # The history: alerts opening and closing, and the group's events (see virgil.events).
                "events": list(self.events.recent)[:50],
                "alerts": None if self.alerts is None else {
                    "supported": self.alerts.supported,
                    "open": self.alerts.open_summaries(),
                    "recovering": self.alerts.recovering_summaries(),
                    "recent": list(self.events.recent)[:50],
                },
                "dhcp_names": dhcp_lease_names(),
                "trim_ppb": self.config.trim_ppb,
                "ntp_hwtimestamp": (self.timing.request.ntp_hwtimestamp
                                    if self.timing is not None and self.timing.request is not None else None),
                "auto_trim": {"enabled": self.config.auto_trim, "last": self.last_trim, "paused": self.trim_paused,
                              "paused_at": self.trim_paused_wall},
                "serving": {
                    **self.serving(),
                    "v1_mode": self.config.ptpv1,
                    "v1_timestamps": self.ptpv1_server.timestamps() if self.ptpv1_server else None,
                    "v1_hardware_tx": self.ptpv1_server.hardware_tx if self.ptpv1_server else None,
                    "switching": self.ptpv1_switching is not None and not self.ptpv1_switching.done(),
                    "v1_followers": self.ptpv1_server.current_followers() if self.ptpv1_server else [],
                },
                # A newer release, if GitHub has told us of one (see virgil.updates).
                "updates": {"enabled": self.config.check_updates, "running": __version__, **self.update_check},
                "ptp": {
                    "interface": self.config.ptp_interface,
                    "tx_slots": self.ptp_slots().as_dict() if self.config.ptp_interface else None,
                    "probe_timestamps": self.config.probe_timestamps,
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
                    "interface": self.netaudio_interface,
                },
                "dante_devices": self.consoles.summaries() if self.consoles else [],
                "device_settings": {
                    mac: {"native_ptpv2": d.native_ptpv2, "aes67": d.aes67} for mac, d in self.config.devices.items()
                },
                "web_certificate": None if self.web_certificate is None else {
                    **self.web_certificate,
                    "days_left": round((self.web_certificate["not_after"] - time.time()) / 86400, 1),
                },
                "recording": None if self.recording is None else {
                    **self.recording.state(),
                    # Whether this Virgil is coordinating it; a member's page says where to stop it.
                    "here": self.recording_here,
                },
                "other_virgils": others,
            }
        )

    async def join(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "the request must be JSON"}, status=400)
        if not self.clock_settled():
            # The newcomer is stamped by this Virgil's clock, and takes on its settings (see virgil.shared).
            return web.json_response({"error": shared.UNSETTLED}, status=409)
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
        self.events.group("joined", f"{body.get('name')} joined the group, through this Virgil")
        await self._update_advert()
        # Everyone else learns of the newcomer now (and from heartbeats, if they miss this).
        spawn(self.push_members([i for i in self.member_ids() if i != body.get("id")]),
              "Telling the members about a newcomer")
        return web.json_response(reply)

    def public_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024, middlewares=[security_headers])
        app.router.add_get("/", self.page)
        app.router.add_get("/static/{name}", self.static_file)
        app.router.add_get("/api/v1/status", self.status)
        app.router.add_get("/api/v1/graphs", self.graphs_api)
        app.router.add_get("/api/v1/time", self.time_api)
        app.router.add_get(MEMBERSHIP_PATH, self.membership_api)
        app.router.add_post(JOIN_PATH, self.join)
        self.web = WebApi(self)
        self.web.routes(app)
        self.recording = Session(
            self.config.name, self.status_data, lambda window: self.graphs.window(window, time.time()),
            lambda: dict(self.consoles.diagnostics) if self.consoles else {}, root=self.recordings_root,
            events=lambda since: self.events.since(since))
        self.recording.on_auto_stop = self.stop_recording  # a session that stops itself stops as a whole
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
            spawn(self._update_advert(), "Updating the mDNS advert")

    def update_dhcp_names(self, systemd: roles.Systemd | None = None, path: Path = DHCP_NAMES_REQUEST,
                          add: list[str] | None = None, force: bool = False) -> None:
        """Ask virgil-root to set up each chosen interface's DHCP lease, if the interfaces have changed.

        add names interfaces that ifupdown doesn't configure yet, which
        someone has agreed virgil-root should set up to come up.
        """
        if not self.manage_timing:
            return
        wanted = (self.config.ptp_interface, self.config.mgmt_interface)
        if wanted == self.dhcp_names_for and not add and not force:
            return
        request = {"ptp_interface": wanted[0], "mgmt_interface": wanted[1], "add": list(add or [])}
        try:
            atomic_write(path, json.dumps(request).encode(), mode=0o640)
            (systemd or roles.Systemd()).start(DHCP_NAMES_UNIT, block=False)
        except (OSError, roles.ServiceError) as error:
            log.warning("Couldn't update the DHCP lease names: %s", error)
            return
        self.dhcp_names_for = wanted

    async def bind_netaudio_ui(self) -> None:
        """Listen for netaudio's UI on the management addresses and loopback only, following them as they change.

        Loopback stays so an SSH tunnel still works; the PTP interface never
        gets it, since the consoles' network has no business with it.
        """
        if self.netaudio_ui is None:
            return
        wanted = {"127.0.0.1", *self.addresses}
        for address in list(self.netaudio_ui_sites):
            if address not in wanted:
                await self.netaudio_ui_sites.pop(address).stop()
        for address in sorted(wanted - set(self.netaudio_ui_sites)):
            site = web.TCPSite(self.netaudio_ui, address, NETAUDIO_PROXY_PORT, ssl_context=self.netaudio_ui_context)
            try:
                await site.start()
            except OSError as error:
                log.warning("Couldn't serve netaudio's UI on %s:%d: %s", address, NETAUDIO_PROXY_PORT, error)
                continue
            self.netaudio_ui_sites[address] = site

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
        self.events.group("timing", "Timing was stopped on this Virgil")
        return True

    def start_timing_again(self) -> bool:
        if self.timing is None:
            return False
        self._log_change(self.election.start(time.monotonic()))
        self.events.group("timing", "Timing was started again on this Virgil")
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
        app.router.add_get(GRAPHS_PATH, self.group_graphs)
        app.router.add_post(RECORDING_PATH, self.group_recording)
        app.router.add_post(SETTIME_PATH, self.group_settime)
        app.router.add_post(DEVICE_PATH, self.group_device)
        app.router.add_get(MEMBERS_PATH, self.group_members_get)
        app.router.add_post(MEMBERS_PATH, self.group_members_post)
        app.router.add_post(RESYNC_PATH, self.group_resync)
        app.router.add_get(RECORDINGS_PATH + "/{name}", self.group_recording_file)
        app.router.add_post(RECORDINGS_PATH + "/delete", self.group_recording_delete)
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
        app.router.add_post(REMOVE_PATH, self.control_remove)
        app.router.add_post(LEAVE_PATH, self.control_leave)
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


_GONE = object()


def _merge(before: dict, ours: dict, theirs: dict) -> dict:
    """A three-way merge of config (as config.to_dict gives it): `theirs`, with everything `ours` changed since
    `before` laid over it. Tables are merged setting by setting, so two changes to different members of the
    group, or an invite made by virgilctl and a member added by virgild, both survive."""
    merged = dict(theirs)
    for key in before.keys() | ours.keys():
        was, now = before.get(key, _GONE), ours.get(key, _GONE)
        if was == now:
            continue  # we didn't change it: theirs stands
        if now is _GONE:
            merged.pop(key, None)  # we removed it
        elif isinstance(was, dict) and isinstance(now, dict) and isinstance(theirs.get(key), dict):
            merged[key] = _merge(was, now, theirs[key])
        else:
            merged[key] = now
    return merged


def dhcp_lease_names() -> dict[str, str]:
    """The DHCP lease name each kind of interface asks for."""
    from virgil.dhcpnames import host_label

    hostname = socket.gethostname()
    return {"ptp": host_label(hostname, "ptp"), "mgmt": host_label(hostname, "mgmt")}


async def start_extras(daemon: Daemon, host: str, public: web.AppRunner) -> list[web.AppRunner]:
    """The web UI on 443, a redirect from HTTP on 80, and netaudio's UI. None of them is essential."""
    runners: list[web.AppRunner] = []
    # The ports people browse to use the web certificate, if one was imported (see tls.web_identity).
    context = tls.web_server_context(daemon.identity, daemon.identity.cert_path.parent)
    web_identity = tls.web_identity(daemon.identity.cert_path.parent)
    if web_identity is not None:
        try:
            daemon.web_certificate = tls.inspect(web_identity.cert_path)
            log.info("Using the web certificate for %s, issued by %s", daemon.web_certificate["subject"],
                     daemon.web_certificate["issuer"])
        except tls.TlsError as error:
            log.warning("Couldn't read the web certificate: %s", error)
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
        daemon.netaudio_ui, daemon.netaudio_ui_context = netaudio, context
        await daemon.bind_netaudio_ui()
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
            daemon.tidy_ptp(), daemon.watch_consoles(), daemon.watch_clock(), daemon.watch_trim(),
            daemon.watch_alerts(), daemon.watch_graphs(Pinger(daemon.on_ping), daemon.dante_pinger()), daemon.watch_ptpv1(),
            daemon.watch_membership(), daemon.watch_updates()]
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
        if daemon.ptpv1_server is not None:
            await daemon.ptpv1_server.stop()
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
