#!/usr/bin/env python3
"""Record everything worth having from a Virgil during a test session.

Run it on each Virgil, with sudo (it reads the journal):

    sudo python3 ~/dante-virgil/tools/virgil-record.py

It writes to ~/virgil-record-<host>-<time>/ until you press Ctrl+C:

    status.jsonl       virgild's status every 2 s: clocks, roles, consoles, members
    devices.jsonl      netaudio's /devices every 10 s
    diagnostics.jsonl  netaudio's /diagnostics for every device every 30 s
    ping.log           round-trip time to each other member, once a second
    journal.log        virgild, chrony, ptp4l, phc2sys and netaudio's logs
    notes.log          your notes (see below)

To note something, with the exact time, from another terminal on the same Virgil:

    python3 ~/dante-virgil/tools/virgil-record.py note "pulled the radio"

When you stop it, it compresses everything into one .tar.gz to send back.
Only standard Python is used, so it works offline.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

STATUS_URL = "https://127.0.0.1:8740/api/v1/status"
NETAUDIO = "http://127.0.0.1:4780"
UNITS = ["dante-virgil", "dante-virgil-chrony", "dante-virgil-ptp4l", "dante-virgil-phc2sys",
         "dante-virgil-netaudio", "dante-virgil-trim", "dante-virgil-sync"]
NOTES_POINTER = Path("/tmp/virgil-record-current")


def home() -> Path:
    # Under sudo, write to the person's own home, not root's.
    user = os.environ.get("SUDO_USER")
    return Path(os.path.expanduser(f"~{user}")) if user else Path.home()


def get_json(url: str, timeout: float = 5.0):
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # virgild on this machine, over loopback
    with urllib.request.urlopen(url, timeout=timeout, context=context) as reply:
        return json.load(reply)


def append(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def note(text: str) -> int:
    try:
        folder = Path(NOTES_POINTER.read_text().strip())
    except OSError:
        print("No recording is running on this Virgil.", file=sys.stderr)
        return 1
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
    with (folder / "notes.log").open("a") as f:
        f.write(f"{stamp} {text}\n")
    print(f"{stamp} {text}")
    return 0


def record() -> int:
    started = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = home() / f"virgil-record-{socket.gethostname()}-{started}"
    folder.mkdir(parents=True)
    NOTES_POINTER.write_text(str(folder))
    print(f"Recording to {folder}. Press Ctrl+C to stop.")

    journal = subprocess.Popen(
        ["journalctl", "-f", "-n", "0", "-o", "short-iso-precise", *[a for u in UNITS for a in ("-u", u)]],
        stdout=(folder / "journal.log").open("w"), stderr=subprocess.DEVNULL)
    pings: dict[str, subprocess.Popen] = {}
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    counts = {"status": 0, "devices": 0, "diagnostics": 0, "errors": 0}
    next_devices = next_diagnostics = 0.0
    while not stopping:
        now = time.time()
        try:
            status = get_json(STATUS_URL)
            append(folder / "status.jsonl", {"t": now, "status": status})
            counts["status"] += 1
            # Ping every other member, once a second, for the round trip across the link.
            for member in (status.get("group") or {}).get("members", []):
                for address in member.get("addresses") or []:
                    if address not in pings:
                        pings[address] = subprocess.Popen(
                            ["ping", "-D", "-i", "1", address],
                            stdout=(folder / f"ping-{address}.log").open("w"), stderr=subprocess.STDOUT)
        except Exception as error:  # keep recording whatever happens
            append(folder / "errors.jsonl", {"t": now, "what": "status", "error": str(error)})
            counts["errors"] += 1
        if now >= next_devices:
            next_devices = now + 10
            try:
                devices = get_json(f"{NETAUDIO}/devices")
                append(folder / "devices.jsonl", {"t": now, "devices": devices})
                counts["devices"] += 1
                if now >= next_diagnostics:
                    next_diagnostics = now + 30
                    for server in devices if isinstance(devices, dict) else []:
                        url = f"{NETAUDIO}/diagnostics/{urllib.parse.quote(server, safe='')}"
                        append(folder / "diagnostics.jsonl", {"t": now, "device": server, "diagnostics": get_json(url)})
                        counts["diagnostics"] += 1
            except Exception as error:
                append(folder / "errors.jsonl", {"t": now, "what": "netaudio", "error": str(error)})
                counts["errors"] += 1
        time.sleep(max(0.0, 2.0 - (time.time() - now)))

    print("\nStopping...")
    for process in [journal, *pings.values()]:
        process.terminate()
    NOTES_POINTER.unlink(missing_ok=True)
    archive = folder.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname=folder.name)
    user = os.environ.get("SUDO_USER")
    if user:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(folder), str(archive)], check=False)
    print(f"Recorded {counts['status']} status samples, {counts['devices']} device lists, "
          f"{counts['diagnostics']} diagnostics, with {counts['errors']} errors.")
    print(f"Send back: {archive}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "note":
        sys.exit(note(" ".join(sys.argv[2:])))
    sys.exit(record())
