import asyncio
import json
import tarfile

import pytest

from virgil.recording import Session


def session(tmp_path, name="Box A"):
    return Session(name, lambda: {"name": name, "state": "leader"}, lambda window: [{"key": "trim/Box A"}],
                   lambda: {"10.0.0.5": {"receiver": {"paths": [{"latency": {"points": [[1, 2]], "current": 5}}]}}},
                   root=tmp_path, units=None)


def test_a_recording_from_start_to_archive(tmp_path):
    s = session(tmp_path)

    async def scenario():
        name = await s.start("20261109-140000")
        assert s.state()["active"] and s.state()["name"] == name == "virgil-20261109-140000-Box-A"
        s.note("pulled the radio")
        await asyncio.sleep(0.1)
        assert s.state()["size"] > 0  # status, graphs and diagnostics have been written
        return await s.stop()

    archive = asyncio.run(scenario())
    assert archive == "virgil-20261109-140000-Box-A.tar.gz" and not s.active
    with tarfile.open(tmp_path / archive) as tar:
        files = {m.name.split("/")[-1]: m for m in tar.getmembers() if m.isfile()}
        assert {"status.jsonl", "graphs.jsonl", "diagnostics.jsonl", "notes.log"} <= set(files)
        status = json.loads(tar.extractfile(files["status.jsonl"]).readline())
        diagnostics = json.loads(tar.extractfile(files["diagnostics.jsonl"]).readline())
        notes = tar.extractfile(files["notes.log"]).read().decode()
    assert status["status"]["name"] == "Box A"
    assert "points" not in json.dumps(diagnostics)  # the graphs already have the histories
    assert notes.rstrip().endswith("pulled the radio")
    assert not (tmp_path / "virgil-20261109-140000-Box-A").exists()  # the folder is packed away


def test_recordings_on_disk(tmp_path):
    s = session(tmp_path)
    (tmp_path / "virgil-a.tar.gz").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a recording")
    assert [r["name"] for r in s.recordings()] == ["virgil-a.tar.gz"]
    for bad in ("../etc/passwd", "notes.txt", "missing.tar.gz", ".hidden.tar.gz"):
        with pytest.raises(FileNotFoundError):
            s.path(bad)
    s.delete("virgil-a.tar.gz")
    assert s.recordings() == []


def test_one_at_a_time_and_notes_need_a_recording(tmp_path):
    s = session(tmp_path)
    with pytest.raises(RuntimeError):
        s.note("too early")

    async def scenario():
        await s.start()
        with pytest.raises(RuntimeError):
            await s.start()
        await s.stop()
        assert await s.stop() is None

    asyncio.run(scenario())


def test_status_is_logged_without_the_event_history_which_is_written_once(tmp_path):
    """Repeated in every status reading, the event history was 97% of a night's log."""
    history = [{"t": 1795152800.0, "event": "group", "label": "NEW LEADER", "title": "Box B became the leader"}]
    status = {"name": "Box A", "events": history * 50,
              "alerts": {"supported": True, "open": [{"title": "Late packets"}], "recent": history * 50}}
    s = Session("Box A", lambda: status, lambda window: [], lambda: {}, root=tmp_path, units=None,
                events=lambda since: history)

    async def scenario():
        await s.start("20261120-013227")
        await asyncio.sleep(0.1)
        return await s.stop()

    archive = asyncio.run(scenario())
    with tarfile.open(tmp_path / archive) as tar:
        files = {m.name.split("/")[-1]: m for m in tar.getmembers() if m.isfile()}
        logged = json.loads(tar.extractfile(files["status.jsonl"]).readline())["status"]
        events = [json.loads(line) for line in tar.extractfile(files["events.jsonl"]).read().splitlines()]
    assert "events" not in logged and "recent" not in logged["alerts"]
    assert logged["alerts"]["open"] == [{"title": "Late packets"}]  # what's open now is still in every reading
    assert events == history


def test_diagnostics_are_logged_every_five_minutes():
    from virgil.recording import DIAGNOSTICS_EVERY, GRAPHS_EVERY, STATUS_EVERY

    assert DIAGNOSTICS_EVERY * STATUS_EVERY == 300 and GRAPHS_EVERY * STATUS_EVERY == 30


def test_packing_falls_back_when_tar_cant_run(tmp_path, monkeypatch):
    import virgil.recording as recording

    folder = tmp_path / "virgil-20261120-013227-Box-A"
    folder.mkdir()
    (folder / "status.jsonl").write_text('{"t": 1}\n')
    for command in (recording.PACK, ["no-such-command-here"]):
        monkeypatch.setattr(recording, "PACK", command)
        archive = tmp_path / f"{len(command)}.tar.gz"
        asyncio.run(recording.pack(folder, archive))
        with tarfile.open(archive) as tar:
            assert "virgil-20261120-013227-Box-A/status.jsonl" in tar.getnames()
