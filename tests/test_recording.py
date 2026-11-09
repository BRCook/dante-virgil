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
