"""The web UI's actions: everything that changes something, behind the password.

Every action is a POST that needs a valid session cookie and the header
X-Virgil-Request. Browsers won't let another site send that header without
permission, so with the session cookie only sent by the Virgil's own pages,
no other web page can act on someone's behalf. A request whose Origin is
another site is refused outright.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from virgil import group as groups
from virgil import joining, shared
from virgil.config import DeviceSettings
from virgil.consoles import normalise_mac
from virgil.group import GroupError, InviteToken
from virgil.setup import SetupError, check_mgmt_interface, check_ptp_interface, configured, describe_interfaces
from virgil.webauth import (COOKIE, SESSION_LIFETIME, Attempts, Sessions, check_password, hash_password,
                            weaker_than_strong)

if TYPE_CHECKING:
    from virgil.daemon import Daemon

log = logging.getLogger("virgild.web")

HEADER = "X-Virgil-Request"


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


DEVICE_PATH = "/group/v1/device"


class WebApi:
    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.sessions = Sessions()
        self.attempts = Attempts()
        # Checking a password takes a second or so and 32 MiB with strong hashes: one at a time, so simultaneous
        # logins can't each take that at once.
        self._checking = asyncio.Semaphore(1)

    # Checks

    def logged_in(self, request: web.Request) -> bool:
        return self.sessions.valid(request.cookies.get(COOKIE))

    @staticmethod
    def _same_site(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        return origin is None or origin == f"https://{request.host}"

    async def _body(self, request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def guarded(self, handler):
        """Wrap an action so it needs a session, the header, and a same-site origin."""

        async def checked(request: web.Request) -> web.Response:
            if request.headers.get(HEADER) != "1" or not self._same_site(request):
                return _error("refused: this request didn't come from Virgil's own page", 403)
            if not self.logged_in(request):
                return _error("log in to change things", 401)
            return await handler(request, await self._body(request))

        return checked

    # Logging in and out

    async def session(self, request: web.Request) -> web.Response:
        return web.json_response({
            "logged_in": self.logged_in(request),
            "password_set": bool(self.daemon.config.web_password),
        })

    async def login(self, request: web.Request) -> web.Response:
        if request.headers.get(HEADER) != "1" or not self._same_site(request):
            return _error("refused: this request didn't come from Virgil's own page", 403)
        if not self.daemon.config.web_password:
            return _error("no password is set. On this Virgil, run: sudo virgilctl password", 409)
        address = request.remote or "unknown"
        if self.attempts.blocked(address):
            return _error("too many wrong passwords; wait a minute", 429)
        body = await self._body(request)
        password = body.get("password")
        # Checking a password takes a moment; don't hold up everything else.
        stored = self.daemon.config.web_password
        async with self._checking:
            ok = isinstance(password, str) and await asyncio.to_thread(check_password, password, stored)
            if ok:
                await self._strengthen(password, stored)
        if not ok:
            self.attempts.failed(address)
            log.warning("Wrong web password from %s", address)
            return _error("wrong password", 401)
        self.attempts.succeeded(address)
        response = web.json_response({"logged_in": True})
        response.set_cookie(COOKIE, self.sessions.create(), max_age=SESSION_LIFETIME, path="/",
                            secure=True, httponly=True, samesite="Strict")
        return response

    async def _strengthen(self, password: str, stored: str) -> None:
        """With the password in hand, upgrade a hash made with 1.x's settings, once the whole group can check strong
        ones (see virgil.features). Only the leader does this (see Daemon.acts_for_group). It spreads like a new
        password."""
        if ("strong_hashes" not in self.daemon.config.features or not weaker_than_strong(stored)
                or not self.daemon.acts_for_group() or self.daemon.config.web_password != stored):
            return
        self.daemon.config.web_password = await asyncio.to_thread(hash_password, password, True)
        self.daemon.shared_setting_changed_here("web_password", self.daemon.config.web_password,
                                                "strengthening its hash on a login", describe_as=
                                                "strengthened the web UI's password hash for the group")
        self.daemon.save()

    async def logout(self, request: web.Request) -> web.Response:
        self.sessions.end(request.cookies.get(COOKIE))
        response = web.json_response({"logged_in": False})
        response.del_cookie(COOKIE, path="/")
        return response

    # Actions

    async def lead(self, request: web.Request, body: dict) -> web.Response:
        status, reply = await self.daemon.request_lead()
        return web.json_response(reply, status=status)

    async def stop(self, request: web.Request, body: dict) -> web.Response:
        if not self.daemon.stop_timing():
            return _error("virgild doesn't manage roles here (observe_only is set)", 409)
        return web.json_response({"message": f"{self.daemon.config.name} is stopping its timing services"})

    async def start(self, request: web.Request, body: dict) -> web.Response:
        if not self.daemon.start_timing_again():
            return _error("virgild doesn't manage roles here (observe_only is set)", 409)
        return web.json_response({"message": f"{self.daemon.config.name} is starting again"})

    async def trim(self, request: web.Request, body: dict) -> web.Response:
        ppm = body.get("ppm")
        if isinstance(ppm, bool) or not isinstance(ppm, (int, float)) or abs(ppm) > 1000:
            return _error("the trim must be a number of ppm, within ±1000", 400)
        self.daemon.config.trim_ppb = round(ppm * 1000)
        self.daemon.save()
        if self.daemon.timing is not None:
            self.daemon.timing.poke()
        return web.json_response({"trim_ppb": self.daemon.config.trim_ppb})

    async def auto_trim(self, request: web.Request, body: dict) -> web.Response:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _error("enabled must be true or false", 400)
        if enabled != self.daemon.config.auto_trim:
            if not self.daemon.clock_settled():
                return _error(shared.UNSETTLED, 409)
            self.daemon.shared_setting_changed_here("auto_trim", enabled, "from its page")
        self.daemon.config.auto_trim = enabled
        if not enabled:
            self.daemon.trim_paused = None
            self.daemon.trim_history.clear()
        self.daemon.save()
        return web.json_response({"auto_trim": enabled})

    async def device(self, request: web.Request, body: dict) -> web.Response:
        """Change a console's enforce setting: this Virgil's own, or a member's, by asking that member."""
        mac = normalise_mac(body.get("mac"))
        setting, enabled, virgil = body.get("setting"), body.get("enabled"), body.get("virgil")
        if mac is None or setting not in ("ptpv2", "aes67") or not isinstance(enabled, bool):
            return _error("needs a MAC address, a setting (ptpv2 or aes67), and enabled", 400)
        if virgil in (None, self.daemon.config.id):
            return web.json_response(self.daemon.set_device(mac, setting, enabled))
        group = self.daemon.config.group
        if not isinstance(virgil, str) or group is None or virgil not in group.members:
            return _error("that console's Virgil isn't a member of this group", 404)
        reply = await self.daemon._member_call(virgil, "POST", DEVICE_PATH,
                                               {"mac": mac, "setting": setting, "enabled": enabled})
        if reply is None:
            return _error(f"{group.members[virgil].name} didn't answer, so its console wasn't changed", 503)
        return web.json_response(reply)

    async def invite(self, request: web.Request, body: dict) -> web.Response:
        config = self.daemon.config
        if config.group is None:
            return _error("this Virgil isn't in a group", 409)
        if not self.daemon.addresses:
            return _error("the management interface has no address to put in an invite", 409)
        minutes = body.get("minutes", 60)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 24 * 60:
            return _error("minutes must be between 1 and 1440", 400)
        secret, expires = groups.create_invite(config.group, time.time(), lifetime=minutes * 60)
        self.daemon.save()
        token = InviteToken(config.group.id, tuple(self.daemon.addresses), self.daemon.public_port,
                            self.daemon.identity_fingerprint(), secret, expires)
        return web.json_response({"token": token.encode(), "expires": expires,
                                  "invite_id": groups.invite_id(secret, is_secret=True)})

    async def create_group(self, request: web.Request, body: dict) -> web.Response:
        config = self.daemon.config
        if config.group is not None:
            return _error("this Virgil is already in a group", 409)
        config.group = groups.create(config.id, config.name, self.daemon.identity.cert_pem, time.time())
        self.daemon.save()
        self.daemon.group_changed()
        return web.json_response({"group_id": config.group.id})

    async def join(self, request: web.Request, body: dict) -> web.Response:
        try:
            token = InviteToken.decode(str(body.get("token", "")))
            updated = copy.deepcopy(self.daemon.config)
            await joining.join(token, updated, self.daemon.identity.cert_pem)
        except GroupError as error:
            return _error(str(error), 400)
        self.daemon.config = updated
        self.daemon.save()
        self.daemon.group_changed()
        names = sorted(m.name for i, m in updated.group.members.items() if i != updated.id)
        return web.json_response({"group_id": updated.group.id, "members": names})

    async def interfaces(self, request: web.Request) -> web.Response:
        return web.json_response({
            "interfaces": describe_interfaces(),
            "ptp_interface": self.daemon.config.ptp_interface,
            "mgmt_interface": self.daemon.config.mgmt_interface,
        })

    async def choose_interfaces(self, request: web.Request, body: dict) -> web.Response:
        """Choose the PTP and management interfaces, then restart virgild to use them."""
        config = self.daemon.config
        ptp, mgmt = body.get("ptp_interface"), body.get("mgmt_interface")
        accept_profile, accept_software = body.get("accept_profile"), body.get("accept_software", False)
        if not isinstance(ptp, str) or not isinstance(mgmt, str):
            return _error("needs a PTP interface and a management interface", 400)
        if accept_profile is not None and not isinstance(accept_profile, bool):
            return _error("accept_profile must be true or false", 400)
        try:
            choice = check_ptp_interface(ptp, accept_profile, accept_software is True)
            check_mgmt_interface(mgmt)
        except SetupError as error:
            return _error(str(error), 400)
        if choice.needs:
            # Someone has to agree to this first; the page asks, then sends it again.
            return web.json_response({"needs": choice.needs, "message": choice.message}, status=409)
        known = configured()
        missing = [] if known is None else sorted({choice.name, mgmt} - known)
        add_stanzas = body.get("add_stanzas")
        if missing and add_stanzas is None:
            names = " and ".join(missing)
            return web.json_response({"needs": "stanza", "interfaces": missing, "message": (
                f"{names} {'isn' if len(missing) == 1 else 'aren'}'t set up in the OS (there's no setup for "
                f"{'it' if len(missing) == 1 else 'them'} in /etc/network/interfaces), so "
                f"{'it' if len(missing) == 1 else 'they'} won't have an address after a reboot. Add a standard DHCP "
                f"setup for {'it' if len(missing) == 1 else 'them'}?")}, status=409)
        if (choice.name, choice.nic_profile, mgmt) == (config.ptp_interface, config.nic_profile, config.mgmt_interface):
            if missing and add_stanzas:
                self.daemon.update_dhcp_names(add=missing, force=True)
                return web.json_response({"message": f"Setting up {' and '.join(missing)} to come up"})
            return web.json_response({"message": "Nothing changed"})
        config.ptp_interface, config.nic_profile, config.mgmt_interface = choice.name, choice.nic_profile, mgmt
        self.daemon.save()
        if missing and add_stanzas:
            self.daemon.update_dhcp_names(add=missing)
        log.info("Interfaces changed from the web UI (PTP %s, management %s); restarting", choice.name, mgmt)
        self.daemon.request_restart()
        message = "Saved. Virgil is restarting to use them."
        return web.json_response({"message": f"{choice.note} {message}" if choice.note else message,
                                  "restarting": True})

    # PTPv1

    async def set_ptpv1(self, request: web.Request, body: dict) -> web.Response:
        from virgil.config import PTPV1_MODES

        mode = body.get("mode")
        if mode not in PTPV1_MODES:
            return _error(f"mode must be one of {', '.join(PTPV1_MODES)}", 400)
        old, self.daemon.config.ptpv1 = self.daemon.config.ptpv1, mode
        self.daemon.save()
        self.daemon.change_ptpv1_mode(old, mode)  # someone asked: no need to wait for the usual hold
        return web.json_response({"mode": mode, **self.daemon.serving()})

    async def set_check_updates(self, request: web.Request, body: dict) -> web.Response:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _error("enabled must be true or false", 400)
        turned_on = enabled and not self.daemon.config.check_updates
        self.daemon.config.check_updates = enabled
        self.daemon.save()
        if turned_on:
            self.daemon.wake_update_check()  # check now, not tomorrow
        return web.json_response({"check_updates": enabled})

    async def set_probe_timestamps(self, request: web.Request, body: dict) -> web.Response:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _error("enabled must be true or false", 400)
        self.daemon.config.probe_timestamps = enabled
        self.daemon.save()
        return web.json_response({"probe_timestamps": enabled})

    # Removing a member, and leaving

    async def remove_member(self, request: web.Request, body: dict) -> web.Response:
        virgil_id = body.get("id")
        if not isinstance(virgil_id, str):
            return _error("say which member to remove", 400)
        try:
            return web.json_response({"removed": await self.daemon.remove_member(virgil_id)})
        except RuntimeError as error:
            return _error(str(error), 409)

    async def leave_group(self, request: web.Request, body: dict) -> web.Response:
        try:
            await self.daemon.leave_group()
        except RuntimeError as error:
            return _error(str(error), 409)
        return web.json_response({"left": True})

    # Time

    async def sync_time(self, request: web.Request, body: dict) -> web.Response:
        """Set the group's time from the browser's. The page has already warned that audio stops."""
        delta = body.get("delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or abs(delta) > 20 * 365 * 86400:
            return _error("delta must be a number of seconds", 400)
        if abs(delta) < 0.5:
            return _error("this Virgil's clock already agrees with your device", 409)
        try:
            return web.json_response(await self.daemon.set_group_time(float(delta)))
        except (RuntimeError, OSError) as error:
            return _error(str(error), 409)

    # Recordings

    async def start_recording(self, request: web.Request, body: dict) -> web.Response:
        if self.daemon.recording.active:
            return _error("already logging", 409)
        return web.json_response(await self.daemon.start_recording(group=body.get("group", True) is not False))

    async def stop_recording(self, request: web.Request, body: dict) -> web.Response:
        result = await self.daemon.stop_recording()
        if result["archive"] is None:
            return _error("not logging", 409)
        return web.json_response({**result, "download": f"/api/v1/recordings/{result['archive']}"})

    async def recording_note(self, request: web.Request, body: dict) -> web.Response:
        text = str(body.get("text", "")).strip()
        if not text or len(text) > 500:
            return _error("a note needs some text (at most 500 characters)", 400)
        try:
            return web.json_response({"at": self.daemon.recording.note(text)})
        except RuntimeError:
            return _error("not logging", 409)

    async def delete_recordings(self, request: web.Request, body: dict) -> web.Response:
        names = [r["name"] for r in self.daemon.recording.recordings()] if body.get("all") else [body.get("name", "")]
        for name in names:
            try:
                self.daemon.recording.delete(str(name))
            except FileNotFoundError:
                return _error(f"no recording called {name}", 404)
            self.daemon.replays.forget(str(name))  # and its copy prepared for replay, if any
        return web.json_response({"deleted": len(names)})

    async def start_replay(self, request: web.Request, body: dict) -> web.Response:
        try:
            archive = self.daemon.recording.path(str(body.get("name", "")))
            archive.stat()
        except (FileNotFoundError, ValueError):
            return _error("no such recording", 404)
        return web.json_response(await self.daemon.replays.prepare(archive))

    def _replay_name(self, request: web.Request) -> str | web.Response:
        if not self.logged_in(request):
            return _error("log in to replay recordings", 401)
        name = request.query.get("name", "")
        try:
            self.daemon.recording.path(name).stat()
        except (FileNotFoundError, ValueError):
            return _error("no such recording", 404)
        return name

    async def replay_state(self, request: web.Request) -> web.Response:
        name = self._replay_name(request)
        if isinstance(name, web.Response):
            return name
        state = self.daemon.replays.state(name)
        if state["state"] == "ready":
            state["overview"] = await asyncio.to_thread(self.daemon.replays.overview, name)
        return web.json_response(state)

    async def replay_at(self, request: web.Request) -> web.Response:
        name = self._replay_name(request)
        if isinstance(name, web.Response):
            return name
        try:
            t, window = float(request.query["t"]), float(request.query.get("window", 300))
        except (KeyError, ValueError):
            return _error("give the moment as t (seconds since the epoch), and optionally a window", 400)
        moment = await asyncio.to_thread(self.daemon.replays.at, name, t, window)
        if moment is None:
            return _error("this recording isn't prepared for replay yet", 409)
        return web.json_response(moment)

    async def recording_summary(self, request: web.Request) -> web.Response:
        if not self.logged_in(request):
            return _error("log in to see recordings", 401)
        try:
            summary = await asyncio.to_thread(self.daemon.recording.summary, request.match_info["name"])
        except FileNotFoundError:
            return _error("no such recording", 404)
        if summary is None:
            return _error("this recording has no summary (it was made before Virgil summarised logs)", 404)
        return web.json_response(summary)

    async def list_recordings(self, request: web.Request) -> web.Response:
        if not self.logged_in(request):
            return _error("log in to see recordings", 401)
        return web.json_response({"recordings": self.daemon.recording.recordings()})

    async def download_recording(self, request: web.Request) -> web.StreamResponse:
        if not self.logged_in(request):
            return _error("log in to download recordings", 401)
        name = request.match_info["name"]
        try:
            path = self.daemon.recording.path(name)
        except FileNotFoundError:
            return _error("no such recording", 404)
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/session", self.session)
        app.router.add_get("/api/v1/interfaces", self.interfaces)
        app.router.add_get("/api/v1/recordings", self.list_recordings)
        app.router.add_get("/api/v1/recordings/{name}/summary", self.recording_summary)
        app.router.add_get("/api/v1/replay", self.replay_state)
        app.router.add_get("/api/v1/replay/at", self.replay_at)
        app.router.add_get("/api/v1/recordings/{name}", self.download_recording)
        app.router.add_post("/api/v1/login", self.login)
        app.router.add_post("/api/v1/logout", self.logout)
        for path, handler in (
            ("/api/v1/lead", self.lead),
            ("/api/v1/stop", self.stop),
            ("/api/v1/start", self.start),
            ("/api/v1/trim", self.trim),
            ("/api/v1/auto-trim", self.auto_trim),
            ("/api/v1/device", self.device),
            ("/api/v1/group/create", self.create_group),
            ("/api/v1/group/invite", self.invite),
            ("/api/v1/group/join", self.join),
            ("/api/v1/interfaces", self.choose_interfaces),
            ("/api/v1/time/sync", self.sync_time),
            ("/api/v1/group/remove", self.remove_member),
            ("/api/v1/group/leave", self.leave_group),
            ("/api/v1/ptpv1", self.set_ptpv1),
            ("/api/v1/probe-timestamps", self.set_probe_timestamps),
            ("/api/v1/check-updates", self.set_check_updates),
            ("/api/v1/recording/start", self.start_recording),
            ("/api/v1/recording/stop", self.stop_recording),
            ("/api/v1/recording/note", self.recording_note),
            ("/api/v1/recordings/delete", self.delete_recordings),
            ("/api/v1/replay", self.start_replay),
        ):
            app.router.add_post(path, self.guarded(handler))
