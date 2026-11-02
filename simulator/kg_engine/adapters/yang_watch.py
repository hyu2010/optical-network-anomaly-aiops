"""YANG file watcher — emits `topology.snapshot` on any change.

Design/10 §3 row 1. Polls `mtime` of every JSON file under
`data/yang/` once per second. On any change, reloads the whole Topology via
`src.simulator.yang_loader.load(yang_dir)` and emits a `topology.snapshot`
event carrying the new object. The handler in `handlers/topology.py` diffs
the resulting triple sets to produce a Delta.

Why mtime polling and not `watchdog`/`inotify`: avoids a new dependency,
runs identically on Windows / Linux, and 1 s detection latency is fine for
YANG-level changes (which are minutes to hours apart in practice).

Emits one initial `topology.snapshot` at startup so subscribers see the
full topology graph even when no files change during the engine's run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable

from simulator.simulator import yang_loader

from ..events import Event, EventKind, new_event_id

log = logging.getLogger(__name__)

DEFAULT_YANG_DIR = yang_loader.YANG_DIR


def _mtimes(yang_dir: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not os.path.isdir(yang_dir):
        return out
    for name in os.listdir(yang_dir):
        if name.endswith(".json"):
            p = os.path.join(yang_dir, name)
            try:
                out[name] = os.path.getmtime(p)
            except OSError:
                pass
    return out


class YangWatchAdapter:
    """Adapter — emit `topology.snapshot` events on YANG file changes.

    Parameters
    ----------
    yang_dir : optional override; defaults to `data/yang/`
    poll_interval_s : mtime poll cadence in seconds (default 1.0)
    emit_initial : if True (default), emit one topology.snapshot at startup
    """

    def __init__(self, yang_dir: str | None = None,
                 *, poll_interval_s: float = 1.0,
                 emit_initial: bool = True) -> None:
        self.yang_dir = yang_dir or DEFAULT_YANG_DIR
        self.poll_interval_s = poll_interval_s
        self.emit_initial = emit_initial

    def __call__(self, engine) -> Awaitable[None]:
        return self._run(engine)

    async def _run(self, engine) -> None:
        last = _mtimes(self.yang_dir)
        if self.emit_initial:
            await self._emit(engine)
        while True:
            await asyncio.sleep(self.poll_interval_s)
            current = _mtimes(self.yang_dir)
            if current != last:
                log.info("YANG change detected in %s — reloading", self.yang_dir)
                try:
                    await self._emit(engine)
                except Exception:
                    log.exception("yang reload failed")
                last = current

    async def _emit(self, engine) -> None:
        topo = yang_loader.load(self.yang_dir)
        await engine.submit(Event(
            event_id=new_event_id(),
            kind=EventKind.TOPOLOGY_SNAPSHOT,
            at=time.time() * 1000.0,
            source="yang_watch",
            payload={"topology": topo, "yang_dir": self.yang_dir},
        ))
