"""Append-only NDJSON event log — the engine's source of truth.

Design/10 §6.3, §8. One line per (event, delta) pair. File rotates daily by
UTC date. Every write flushes + fsyncs so a SIGKILL loses at most the
in-flight event.

Schema per line::

    {"event": {"event_id": "...", "kind": "alarm.raise",
               "at": 1700000000000.0, "source": "simulator",
               "payload": {...}},
     "delta": {"adds":    [[subj, pred, obj, src, w, incident_id], ...],
               "removes": [...]}}
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone

from ..events import Delta, Event, Triple


def _triple_to_list(t: Triple) -> list:
    return [t.subject, t.predicate, t.object, t.source, t.weight, t.incident_id]


class EventLogSubscriber:
    """Append (event, delta) NDJSON to a per-day log file.

    Parameters
    ----------
    log_dir : str
        Directory holding the rotating log files. Created if it doesn't exist.
    fsync : bool
        If True (default), `os.fsync` after every write. Set False to trade
        durability for throughput (R-6 will revisit).
    """

    def __init__(self, log_dir: str, *, fsync: bool = True) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        self.fsync = fsync
        self._fp = None
        self._current_date: str | None = None

    def _path_for_today(self) -> str:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        return os.path.join(self.log_dir, f"events.ndjson.{date}"), date

    def _ensure_open(self) -> None:
        path, date = self._path_for_today()
        if self._fp is not None and self._current_date == date:
            return
        if self._fp is not None:
            self._fp.close()
        self._fp = open(path, "a", encoding="utf-8")
        self._current_date = date

    async def on_delta(self, delta: Delta, event: Event) -> None:
        self._ensure_open()
        record = {
            "event": {
                "event_id": event.event_id,
                "kind":     event.kind,
                "at":       event.at,
                "source":   event.source,
                "payload":  event.payload,
            },
            "delta": {
                "adds":    [_triple_to_list(t) for t in delta.adds],
                "removes": [_triple_to_list(t) for t in delta.removes],
            },
        }
        self._fp.write(json.dumps(record, default=str))
        self._fp.write("\n")
        self._fp.flush()
        if self.fsync:
            os.fsync(self._fp.fileno())

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None
