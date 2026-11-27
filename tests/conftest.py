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


@pytest.fixture(autouse=True)
def group_sessions_put_away(monkeypatch):
    """virgild keeps one HTTPS session for its members for as long as it runs, and closes it when it stops. The
    tests start daemons without ever stopping them, each inside its own short-lived event loop, so their sessions
    are left open when that loop has gone; aiohttp would complain about every one at the end of the run. They
    can't be closed properly by then (that needs the loop), so they're marked as closed here instead."""
    from virgil import daemon as module

    made = []
    real = module.Daemon.group_session

    async def group_session(self):
        session = await real(self)
        if not any(session is known for known in made):
            made.append(session)
        return session

    monkeypatch.setattr(module.Daemon, "group_session", group_session)
    yield
    for session in made:
        if not session.closed:
            session.connector._closed = True
