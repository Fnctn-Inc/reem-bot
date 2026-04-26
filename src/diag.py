"""In-memory rolling log of pipeline events.

CF Containers don't give us a CLI logs command, so we expose `/__diag` on
the FastAPI app and have the agent push events into this shared buffer.
Hit `https://reem.fnctn.io/__diag` to see what each pipeline stage emitted.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

_BUFFER: deque = deque(maxlen=400)


def diag(event: str, **kwargs: Any) -> None:
    _BUFFER.append({"t": round(time.time(), 3), "event": event, **kwargs})


def snapshot() -> list[dict]:
    return list(_BUFFER)
