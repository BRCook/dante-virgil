"""A logging session, summed up."""

import json
import statistics
import tarfile

import pytest

from virgil import summary


def write_session(folder, graphs=(), status=(), events=(), notes=""):
    folder.mkdir(parents=True, exist_ok=True)
    for name, records in (("graphs.jsonl", graphs), ("status.jsonl", status), ("events.jsonl", events)):
        (folder / name).write_text("".join(json.dumps(r) + "\n" for r in records))
    (folder / "notes.log").write_text(notes)
    return folder


def test_the_statistics():
    values = [float(v) for v in range(1, 101)]
    stats = summary.statistics_of(values)
    assert (stats["count"], stats["min"], stats["max"], stats["mean"], stats["median"]) == (100, 1, 100, 50.5, 50.5)
    assert stats["stdev"] == pytest.approx(statistics.pstdev(values))
    assert stats["p1"] == pytest.approx(1.99) and stats["p99"] == pytest.approx(99.01)
    assert summary.statistics_of([]) is None


def test_overlapping_windows_count_each_reading_once(tmp_path):
    """Windows of 40 s every 30 s, with times relative to when each was logged, rounded: the same reading can come
    out a few milliseconds different in two windows."""
    def window(t0):
        points = [[round(t - t0, 2) + 0.003, 1000.0] for t in range(int(t0) - 40, int(t0)) for t in [t + 0.25 * q for q in range(4)]]
        return {"t": t0, "series": [{"key": "rtt/A/B", "chart": "rtt", "label": "A ↔ B (Mgmt)", "unit": "ns", "points": points}]}
    folder = write_session(tmp_path / "s", graphs=[window(1000.0 + 30 * n) for n in range(10)])
    rtt = summary.summarise(folder)["measurements"][0]
    assert rtt["count"] == (40 + 30 * 9) * 4  # four a second, from the first window's start to the last's end


def test_late_packets_devices_and_alerts_add_up(tmp_path):
    late = {"t": 1100.0, "series": [{"key": "late/W", "chart": "late", "label": "W", "unit": "per_s",
                                     "points": [[-30.0, 0.0], [-25.0, 2.0], [-20.0, 0.0], [-15.0, 10.0]]}]}
    status = [{"t": 1000.0 + i, "status": {"name": "Box A", "state": "leader", "clock": {"rms_offset_ns": 100 + i},
                                         "dante_devices": [{"name": "WingCompact", "follows_this_virgil": i % 4 != 0,
                                                            "servo": "synchronized", "late_packets": 1996 + i}]}}
              for i in range(8)]
    events = [{"t": 1001.0, "event": "opened", "label": "LOCK LOST", "device": "WingCompact"},
              {"t": 1004.0, "event": "resolved", "label": "LOCK LOST", "device": "WingCompact", "lasted_s": 3.0},
              {"t": 1005.0, "event": "group", "label": "NEW LEADER", "title": "Box A became the leader"}]
    s = summary.summarise(write_session(tmp_path / "s", graphs=[late], status=status, events=events,
                                        notes="2026-11-21T10:00:00.000-04:00 a note\n"))
    assert next(m for m in s["measurements"] if m["chart"] == "late")["total"] == 60.0  # 2/s for 5 s, 10/s for 5 s
    device = s["devices"][0]
    assert device["following_percent"] == 75.0 and device["synchronised_percent"] == 100.0 and device["late_packets"] == 7
    assert s["alerts"] == [{"device": "WingCompact", "label": "LOCK LOST", "times": 1, "seconds": 3.0}]
    assert s["events"][0]["title"] == "Box A became the leader" and s["notes"][0]["text"] == "a note"
    assert s["clock"]["rms_offset_ns"]["max"] == 107 and s["virgil"] == "Box A" and s["states"] == {"leader": 8}


def test_the_page_escapes_what_came_from_the_network(tmp_path):
    status = [{"t": 1000.0, "status": {"name": "Box A", "state": "leader", "clock": {},
                                     "dante_devices": [{"name": "<script>alert(1)</script>", "servo": "synchronized"}]}}]
    page = summary.render([summary.summarise(write_session(tmp_path / "s", status=status))])
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_a_saved_logs_summary_is_found_beside_it_or_inside_it(tmp_path):
    from virgil.recording import Session, summary_path

    session = Session("Box A", lambda: {}, lambda window: [], lambda: {}, root=tmp_path, units=None)
    folder = write_session(tmp_path / "virgil-20261121-100000-Box-A",
                           status=[{"t": 1000.0, "status": {"name": "Box A", "state": "leader", "clock": {}}}])
    summary.write(folder)
    archive = tmp_path / "virgil-20261121-100000-Box-A.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname=folder.name)
    assert session.summary(archive.name)["virgils"][0]["virgil"] == "Box A"  # from inside: no copy beside it yet
    summary_path(archive).write_text(json.dumps({"virgil": "Box A (beside)"}))
    assert session.summary(archive.name)["virgils"][0]["virgil"] == "Box A (beside)"
    session.delete(archive.name)
    assert not summary_path(archive).exists()  # deleting a log deletes its summary too


def test_times_stay_in_ns_in_the_json_and_cycle_units_on_the_page(tmp_path):
    graphs = [{"t": 1100.0, "series": [{"key": "rtt/A/B/dante", "chart": "rtt", "label": "A ↔ B (Dante)", "unit": "ns",
                                        "points": [[-30.0, 674_140.0], [-20.0, 900_000.0]]},
                                       {"key": "console/W", "chart": "console", "label": "W", "unit": "ppm",
                                        "points": [[-30.0, 2.05]]}]}]
    s = summary.summarise(write_session(tmp_path / "s", graphs=graphs))
    rtt = next(m for m in s["measurements"] if m["chart"] == "rtt")
    assert rtt["unit"] == "ns" and rtt["min"] == 674_140.0  # the JSON: always ns, whatever the page shows
    page = summary.render([s])
    assert 'data-ns="674140.0"' in page and "<button type=button" in page  # a time row: a unit button, and its ns
    assert page.count("<button type=button") == 1  # ppm isn't a time: no button


def test_packet_loss_counts_the_pings_lost_and_shows_hundredths(tmp_path):
    points = [[-100.0 + i * 0.25, 100.0 if i in (7, 300) else 0.0] for i in range(400)]  # 2 of 400 pings lost
    graphs = [{"t": 1100.0, "series": [{"key": "loss/A/B/dante", "chart": "loss", "label": "A ↔ B (Dante)",
                                        "unit": "percent", "points": points}]}]
    s = summary.summarise(write_session(tmp_path / "s", graphs=graphs))
    loss = s["measurements"][0]
    assert loss["lost"] == 2 and loss["count"] == 400 and loss["mean"] == 0.5
    page = summary.render([s])
    assert "lost 2 of 400" in page and "<td>0.50</td>" in page and "Ping packet loss" in page


def test_older_summaries_name_their_clock_figures_chrony(tmp_path):
    old = summary.summarise(write_session(tmp_path / "s", status=[
        {"t": 1000.0, "status": {"name": "Box B", "state": "follower", "clock": {"skew_ppm": 0.1}}}]))
    old["chrony"] = old.pop("clock")
    assert "Clock skew" in summary.render([old])
