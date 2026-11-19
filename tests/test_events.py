from virgil.events import EventLog


def test_the_history_outlasts_a_restart(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", clock=lambda: 100.0)
    log.group("joined", "Virgil C joined the group")
    log.record({"t": 101.0, "event": "opened", "label": "LOCK LOST", "device": "WingRack", "title": "Clock lost"})
    reloaded = EventLog(tmp_path / "events.jsonl")
    assert [e["t"] for e in reloaded.recent] == [101.0, 100.0]  # newest first
    assert reloaded.recent[1] == {"t": 100.0, "event": "group", "kind": "joined", "label": "JOINED",
                                  "title": "Virgil C joined the group"}


def test_a_damaged_line_is_skipped(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"t": 5, "event": "group", "title": "fine"}\nnot json\n')
    assert [e["title"] for e in EventLog(path).recent] == ["fine"]
