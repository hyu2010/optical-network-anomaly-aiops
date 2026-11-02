"""Server-Sent Events subscriber — Design/10 §8 (R-4).

Chosen over WebSocket because:
  - One-direction stream (engine → browser) is all we need; SSE is the minimal
    fit and avoids the flask-socketio dependency.
  - SSE survives proxies that strip non-HTTP traffic; WebSockets don't.
  - Browser API `EventSource(url)` is one line; no client library.

Per-reader fan-out: each connected HTTP client gets its own `queue.Queue`.
The subscriber's `on_delta` enqueues a JSON-serialised event record onto
every reader's queue. Slow readers fill up and lose oldest entries (bounded
to 1000 by default) — a single slow browser tab can't back-pressure the
engine.

Thread safety: the engine runs an asyncio loop on whatever thread called
`engine.run()`; the Flask route reads from the queue on the WSGI worker
thread. `queue.Queue` is the standard cross-thread primitive.
"""
from __future__ import annotations

import json
import queue
import threading
from typing import Iterator

from ..events import Event


def _triple_to_list(t):
    return [t.subject, t.predicate, t.object, t.source, t.weight, t.incident_id]


def _serialise(event: Event, delta) -> str:
    return json.dumps({
        "event": {
            "event_id": event.event_id,
            "kind":     event.kind,
            "at":       event.at,
            "source":   event.source,
            "payload":  {k: v for k, v in event.payload.items()
                         if k != "topology"},   # don't serialise the whole topo
        },
        "delta": {
            "adds":    [_triple_to_list(t) for t in delta.adds],
            "removes": [_triple_to_list(t) for t in delta.removes],
        },
    }, default=str)


class SSESubscriber:
    """Fan-out subscriber: each `subscribe()` call returns a fresh iterator
    of SSE-formatted strings ready to write to an HTTP response.
    """

    def __init__(self, max_buffer_per_client: int = 1000) -> None:
        self._lock = threading.Lock()
        self._readers: list[queue.Queue] = []
        self._max = max_buffer_per_client

    async def on_delta(self, delta, event) -> None:
        line = _serialise(event, delta)
        with self._lock:
            for q in list(self._readers):
                # Drop oldest if a reader can't keep up.
                while q.qsize() >= self._max:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                q.put_nowait(line)

    def subscribe(self) -> Iterator[str]:
        """Yield SSE-formatted lines forever. Caller writes each yielded
        string to the HTTP response.

        Stops when the consumer stops iterating (handled by the WSGI server
        closing the response; the subscriber detects this on next put_nowait
        and removes the reader on next on_delta if the queue overflows).
        """
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._readers.append(q)
        try:
            # Send an initial comment so the connection is immediately
            # established on the browser side.
            yield ": connected\n\n"
            while True:
                msg = q.get()
                yield f"data: {msg}\n\n"
        finally:
            with self._lock:
                if q in self._readers:
                    self._readers.remove(q)
