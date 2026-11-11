"""Long-lived background tasks (city tickers, the daily prune, a shutdown countdown).

Such a task is meant to run until it is cancelled or returns. One that ends with an exception would
otherwise go unseen until the task is garbage-collected, if then: ``watch`` logs it when it ends.
"""

import asyncio
import logging
from typing import Any

log = logging.getLogger("opencs.app")


def watch[T: asyncio.Task[Any]](task: T, what: str) -> T:
    """Log ``task``'s exception, naming it ``what``, when it ends with one. Returns ``task``."""

    def ended(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        if (exc := done.exception()) is not None:
            log.error("%s stopped unexpectedly", what, exc_info=exc)

    task.add_done_callback(ended)
    return task
