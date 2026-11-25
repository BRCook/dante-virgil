"""Replaying a saved log."""

import asyncio
import io
import json
import shutil
import tarfile
import time
from pathlib import Path

import pytest

from tests.test_webapi import virgil  # noqa: F401 (the fixture)
from virgil import replay
from virgil.graphs import Recorder, combine_for

START = 1_795_000_000.0


def session(folder: Path, name: str, state: str, seconds: int, latency_ns: float, late_at: float | None = None):
    """A Virgil's session: status every 2 s, graph windows of 40 s every 30 s, as virgil.recording writes them."""
    folder.mkdir(parents=True)
    with (folder / "status.jsonl").open("w") as f:
        for i in range(0, seconds, 2):
            f.write(json.dumps({"t": START + i, "status": {"name": name, "state": state, "tick": i}}) + "\n")
    with (folder / "graphs.jsonl").open("w") as f:
        for end in range(30, seconds + 1, 30):
            series = [{"key": f"latency/{name}/W", "chart": "latency", "label": f"W ({name}) worst flow", "unit": "ns",
                       "points": [[round(t - end, 2), latency_ns + (t % 7) * 1000] for t in range(end - 40, end + 1) if t >= 0]},
                      {"key": f"late/{name}/W", "chart": "late", "label": f"W ({name})", "unit": "per_s",
                       "points": [[round(t - end, 2), 10.0 if late_at is not None and t == late_at else 0.0]
                                  for t in range(end - 40, end + 1, 5) if t >= 0]}]
            f.write(json.dumps({"t": START + end, "series": series}) + "\n")
    (folder / "events.jsonl").write_text(json.dumps({"t": START + 60, "event": "opened", "label": "LOCK LOST",
                                                     "device": "W"}) + "\n")
    return folder


def group_log(tmp_path: Path) -> Path:
    """A group's log, as a leader bundles it: each member's packed log inside a plain .tar."""
    work = tmp_path / "work"
    archives = []
    for name, state, seconds, late_at in (("Box A", "leader", 600, 300), ("Box B", "follower", 540, None)):
        folder = session(work / f"virgil-20261121-100000-{name.replace(' ', '-')}", name, state, seconds, 1.2e6, late_at)
        if name == "Box A":
            (folder / "notes.log").write_text("2026-11-21T10:05:00.000-04:00 baseline\n")
        archive = work / f"{folder.name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(folder, arcname=folder.name)
        archives.append(archive)
    bundle = tmp_path / "virgil-20261121-100000-group.tar"
    with tarfile.open(bundle, "w") as tar:
        for archive in archives:
            tar.add(archive, arcname=archive.name)
    return bundle


def test_a_group_log_prepared_and_replayed(tmp_path):
    bundle = group_log(tmp_path)
    cache = tmp_path / "replay"
    index = replay.prepare(bundle, cache / bundle.name)
    assert [v["virgil"] for v in index["virgils"]] == ["Box A", "Box B"]
    assert (index["from"], index["to"]) == (START, START + 598)
    assert sum(index["overview"]["W (Box A)"]["late"]) == 50  # 10 a second, for the 5 s it covered
    assert [e["virgil"] for e in index["events"]] == ["Box A", "Box B"] and index["notes"][0]["text"] == "baseline"
    replays = replay.Replays(tmp_path, cache)
    assert replays.state(bundle.name) == {"state": "ready"}

    moment = replays.at(bundle.name, START + 300.5, 120)
    assert moment["statuses"]["Box A"]["status"]["tick"] == 300 and moment["statuses"]["Box A"]["age"] == 0.5
    series = {s["key"]: s for s in moment["series"]}
    assert set(series) == {"latency/Box A/W", "late/Box A/W", "latency/Box B/W", "late/Box B/W"}
    assert all(-120 <= p[0] <= 0 for s in moment["series"] for p in s["points"])  # nothing from after the moment

    # The same as the live page would draw, from the same readings.
    live = Recorder()
    for t in range(181, 301):  # (180.5, 300.5]: the readings at whole seconds
        live.add("latency/Box A/W", "latency", "W (Box A) worst flow", "ns", START + t, 1.2e6 + (t % 7) * 1000,
                 combine_for("latency/Box A/W"))
    assert series["latency/Box A/W"]["points"] == live.window(120, START + 300.5)[0]["points"]

    # Box B stopped logging a minute before Box A: at the end, its reading is a minute old.
    end = replays.at(bundle.name, START + 598, 60)
    assert end["statuses"]["Box A"]["age"] == 0 and end["statuses"]["Box B"]["age"] == 60


def test_unpacking_stays_inside_its_folder(tmp_path):
    archive = tmp_path / "virgil-20261121-100000-Box-A.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in (("../outside.txt", b"no"), ("virgil-x/status.jsonl", b"")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("virgil-x/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    folders = replay._unpack(archive, tmp_path / "into")
    assert not (tmp_path / "outside.txt").exists() and not (tmp_path / "into" / "virgil-x" / "link").exists()
    assert folders == [tmp_path / "into" / "virgil-x"]


def test_preparing_in_virgild_one_log_at_a_time(tmp_path, monkeypatch):
    first, second = group_log(tmp_path / "1"), group_log(tmp_path / "2")
    second = second.rename(tmp_path / "virgil-20261121-110000-group.tar")
    replays = replay.Replays(tmp_path, tmp_path / "replay")

    async def ready(name):
        for _ in range(200):
            if replays.state(name)["state"] != "preparing":
                return replays.state(name)
            await asyncio.sleep(0.05)

    async def scenario():
        assert (await replays.prepare(first))["state"] == "preparing"
        assert await ready(first.name) == {"state": "ready"}
        assert replays.overview(first.name)["virgils"] == ["Box A", "Box B"]
        await replays.prepare(second)  # another log: the first is removed
        assert await ready(second.name) == {"state": "ready"}
        assert not (tmp_path / "replay" / first.name).exists()
        replays.forget(second.name)  # as when the log is deleted
        assert replays.state(second.name) == {"state": "none"}

        monkeypatch.setattr(replay.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10**9, 10**9, 1000))
        state = await replays.prepare(first)
        assert state["state"] == "failed" and "disk space" in state["error"]

    asyncio.run(scenario())


def test_replay_through_the_web_api(virgil, tmp_path):
    from tests.test_webapi import HEADERS, _login, run

    async def scenario(daemon, session, base):
        daemon.recordings_root.mkdir(parents=True, exist_ok=True)
        bundle = group_log(tmp_path).rename(daemon.recordings_root / "virgil-20261121-100000-group.tar")
        query = f"name={bundle.name}"
        async with session.get(f"{base}/api/v1/replay?{query}") as reply:
            before = reply.status
        await _login(session, base)
        async with session.post(f"{base}/api/v1/replay", json={"name": bundle.name}, headers=HEADERS) as reply:
            started = (await reply.json())["state"]
        for _ in range(200):
            async with session.get(f"{base}/api/v1/replay?{query}") as reply:
                state = await reply.json()
            if state["state"] != "preparing":
                break
            await asyncio.sleep(0.05)
        async with session.get(f"{base}/api/v1/replay/at?{query}&t={START + 300}&window=60") as reply:
            moment = await reply.json()
        async with session.post(f"{base}/api/v1/recordings/delete", json={"name": bundle.name}, headers=HEADERS):
            pass
        return before, started, state, moment, daemon.replays.state(bundle.name)

    before, started, state, moment, after = run(virgil, scenario)
    assert before == 401 and started == "preparing" and state["state"] == "ready"
    assert state["overview"]["virgils"] == ["Box A", "Box B"]
    assert moment["statuses"]["Box B"]["status"]["state"] == "follower" and moment["window"] == 60
    assert after == {"state": "none"}  # deleting the log forgot its prepared copy
