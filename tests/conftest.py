"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def private_event_history(tmp_path, monkeypatch):
    """Every test daemon records its events in the test's own folder, never in /var/lib/dante-virgil."""
    from virgil import daemon as module

    def events(self):
        if self._event_log is None:
            self._event_log = module.EventLog(tmp_path / f"events-{id(self)}.jsonl")
        return self._event_log

    monkeypatch.setattr(module.Daemon, "events", property(events))


@pytest.fixture(autouse=True)
def private_recordings(tmp_path, monkeypatch):
    """Every test daemon keeps its logs in the test's own folder, never in /var/lib/dante-virgil."""
    from virgil import daemon as module

    created = module.Daemon.__init__

    def init(self, *args, **kwargs):
        created(self, *args, **kwargs)
        self.recordings_root = tmp_path / f"recordings-{id(self)}"

    monkeypatch.setattr(module.Daemon, "__init__", init)
