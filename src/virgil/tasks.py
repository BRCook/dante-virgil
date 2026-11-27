"""Background tasks that are started and left to run.

asyncio itself only keeps a weak reference to a task. One that nothing else holds on to can be garbage-collected
while it's still running, and if it fails, the error is only reported (if at all) when the task is thrown away,
long after the fact. spawn() keeps every task it starts until it finishes, and logs a failure as it happens, so a
background job that dies (the pings, say) says so in the journal instead of just stopping.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("virgild.tasks")

_running: set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("%s failed: %s", task.get_name(), error or type(error).__name__, exc_info=error)


def spawn(coroutine: Coroutine[Any, Any, Any], what: str) -> asyncio.Task:
    """Start a background task, named for the journal. Raises RuntimeError if no event loop is running."""
    try:
        task = asyncio.get_running_loop().create_task(coroutine, name=what)
    except RuntimeError:
        coroutine.close()  # never started, so don't leave it to warn that it was never awaited
        raise
    _running.add(task)
    task.add_done_callback(_finished)
    return task
