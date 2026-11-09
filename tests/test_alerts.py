import asyncio
import json
import logging

import aiohttp
import pytest

from virgil.alerts import Alerts, parse_issues


def issue(kind, severity="error", server="WingCompact.local.", state="open", issue_id=None, summary=""):
    """An issue shaped like netaudio 2.0.0's (monitoring/issues/model.rs)."""
    return {
        "schema_version": 1, "issue_id": issue_id or f"{kind}-{server}", "kind": kind, "severity": severity,
        "state": state, "first_seen": "2026-11-07T21:05:03Z", "last_seen": "2026-11-07T21:05:09Z",
        "resolved_at": None, "occurrence_count": 1, "observation_state": "observed",
        "scope": {"device_identity": "x", "device_name": server.split(".")[0], "server_name": server},
        "title": {"clock_synchronization": "Clock synchronization lost",
                  "clock_muted": "Audio muted by clock state"}.get(kind, kind.replace("_", " ").capitalize()),
        "summary": summary,
    }


def page(*issues):
    return {"schema_version": 1, "count": len(issues), "active_count": len(issues), "issues": list(issues)}


def test_parse():
    (lost, muted) = parse_issues(page(issue("clock_synchronization"), issue("clock_muted", summary="Unlocked")))
    assert lost.label == "LOCK LOST" and lost.device == "WingCompact" and lost.since == "2026-11-07T21:05:03Z"
    assert muted.label == "AUDIO MUTED" and muted.summary == "Unlocked"
    assert parse_issues(page(issue("clock_muted", state="resolved"))) == []
    assert parse_issues({"nope": 1}) == [] and parse_issues(None) == []


class Feed:
    def __init__(self):
        self.page = page()
        self.fail = None

    async def __call__(self):
        if self.fail:
            raise self.fail
        return self.page


def watcher(tmp_path, feed, local=("WingCompact.local.",)):
    now = [1000.0]
    alerts = Alerts(feed, lambda: set(local), events_path=tmp_path / "events.jsonl", clock=lambda: now[0])
    return alerts, now


def test_a_lock_loss_opens_and_resolves(tmp_path, caplog):
    feed = Feed()
    alerts, now = watcher(tmp_path, feed)
    caplog.set_level(logging.INFO)
    feed.page = page(issue("clock_synchronization"))
    asyncio.run(alerts.poll())
    assert [a["label"] for a in alerts.open_summaries()] == ["LOCK LOST"]
    assert "LOCK LOST: WingCompact: Clock synchronization lost" in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    now[0] += 37
    feed.page = page()
    asyncio.run(alerts.poll())
    assert alerts.open_summaries() == []
    assert "Resolved after 37 s" in caplog.text
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["opened", "resolved"] and events[1]["lasted_s"] == 37
    assert [e["event"] for e in alerts.recent] == ["resolved", "opened"]  # newest first


def test_only_local_devices_and_nothing_merely_informational(tmp_path):
    feed = Feed()
    alerts, _ = watcher(tmp_path, feed)
    feed.page = page(issue("clock_muted", server="WingRack.local."),  # another Virgil's console
                     issue("telemetry_stale", severity="info"),
                     issue("receiver_late_packets", severity="warning"))
    asyncio.run(alerts.poll())
    assert [a["label"] for a in alerts.open_summaries()] == ["LATE PACKETS"]


def test_netaudio_going_away_keeps_open_alerts(tmp_path):
    feed = Feed()
    alerts, _ = watcher(tmp_path, feed)
    feed.page = page(issue("clock_muted"))
    asyncio.run(alerts.poll())
    feed.fail = aiohttp.ClientConnectionError("gone")
    asyncio.run(alerts.poll())
    assert len(alerts.open) == 1  # not "resolved" just because netaudio restarted


def test_an_older_netaudio(tmp_path):
    feed = Feed()
    alerts, _ = watcher(tmp_path, feed)
    feed.fail = aiohttp.ClientResponseError(None, (), status=404)
    asyncio.run(alerts.poll())
    assert alerts.supported is False


def test_the_record_is_rotated(tmp_path, monkeypatch):
    import virgil.alerts as module

    monkeypatch.setattr(module, "MAX_EVENTS_BYTES", 200)
    feed = Feed()
    alerts, now = watcher(tmp_path, feed)
    for n in range(6):
        feed.page = page(issue("clock_muted", issue_id=f"m{n}"))
        asyncio.run(alerts.poll())
        now[0] += 1
    assert (tmp_path / "events.jsonl.1").exists()
    assert (tmp_path / "events.jsonl").stat().st_size < 1000


LAST_SEEN = 1794085509.0  # 2026-11-07T21:05:09Z, the last_seen in issue()


def test_an_old_issue_isnt_an_alert(tmp_path):
    """netaudio can keep an issue open long after anyone could see it, such as last night's."""
    feed = Feed()
    alerts, now = watcher(tmp_path, feed)
    now[0] = LAST_SEEN + 12 * 3600  # half a day later
    feed.page = page(issue("clock_synchronization"))
    asyncio.run(alerts.poll())
    assert alerts.open_summaries() == []


def test_an_alert_that_stops_being_seen_goes_quiet(tmp_path, caplog):
    feed = Feed()
    alerts, now = watcher(tmp_path, feed)
    caplog.set_level(logging.INFO)
    now[0] = LAST_SEEN + 10
    feed.page = page(issue("clock_muted"))
    asyncio.run(alerts.poll())
    assert len(alerts.open) == 1
    now[0] = LAST_SEEN + 200  # still "open" to netaudio, but not seen for over 2 minutes
    asyncio.run(alerts.poll())
    assert alerts.open == {}
    assert "Went quiet after" in caplog.text
    assert [e["event"] for e in alerts.recent] == ["went quiet", "opened"]
