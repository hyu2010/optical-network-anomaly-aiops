"""Main loop — queue → dispatch → broadcast.

Design/10 §5. The engine pulls events from an `asyncio.Queue`, looks up the
handler by `event.kind`, runs it under a lock to serialise state mutation,
applies the returned `Delta` to the `TripleIndex`, then broadcasts the delta
to every subscriber.

R-1 handles `alarm.raise`, `incident.boundary`, and ignores topology events
(topology is loaded once at startup via `yang_loader`; mutation events are
R-2 work).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol

from simulator.simulator import yang_loader
from simulator.simulator.topology import Topology

from .events import Delta, Event, EventKind, new_event_id
from .state import StateStore
from . import snapshots as snap_mod

log = logging.getLogger(__name__)

# Handler signature: (event, state) -> Delta. Pure-Python (sync) — no I/O.
Handler = Callable[[Event, StateStore], Delta]


class Subscriber(Protocol):
    async def on_delta(self, delta: Delta, event: Event) -> None: ...


class Engine:
    """Single-process streaming KG engine.

    Construct with an existing `Topology` (or let it load one), then register
    adapters (which push events into `queue`) and subscribers (which receive
    deltas after each event is handled).
    """

    def __init__(self, topology: Topology | None = None,
                 *, snapshots_dir: str | None = None,
                 snapshot_interval_s: float = 60.0,
                 incident_closure_interval_s: float = 30.0,
                 sliding_window: bool = False) -> None:
        # Try to restore from snapshot first; fall back to fresh topology load.
        restored = None
        if snapshots_dir:
            restored = snap_mod.restore(snapshots_dir)
        if restored is not None:
            self.state = restored
            log.info("restored snapshot: %d triples, %d incidents, "
                     "last_event_id=%s",
                     len(self.state.triples.all_triples),
                     len(self.state.incidents),
                     self.state.last_event_id or "<none>")
        else:
            self.state = StateStore(topology=topology or yang_loader.load())
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.subscribers: list[Subscriber] = []
        self.handlers: dict[str, Handler] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._pending_adapters: list = []     # coroutines, started in run()
        self._adapter_tasks: list[asyncio.Task] = []
        self._seen_event_ids: set[str] = set()
        self._snapshots_dir = snapshots_dir
        self._snapshot_interval_s = snapshot_interval_s
        self._incident_closure_interval_s = incident_closure_interval_s
        self._sliding_window = sliding_window
        # Wire the default handler dispatch table.
        from .handlers import default_handlers
        self.handlers.update(default_handlers())

    # -- registration -------------------------------------------------------

    def register_subscriber(self, sub: Subscriber) -> None:
        self.subscribers.append(sub)

    def register_handler(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def register_adapter(self, coro: Awaitable[None]) -> None:
        """Schedule an adapter coroutine to run alongside the main loop.

        Tasks are created lazily inside `run()` so callers can register
        adapters before the event loop is started.
        """
        self._pending_adapters.append(coro)

    # -- ingest -------------------------------------------------------------

    async def submit(self, event: Event) -> None:
        await self.queue.put(event)

    # -- run loop -----------------------------------------------------------

    async def run(self) -> None:
        """Main loop. Returns when stop() is called and the queue drains."""
        for coro in self._pending_adapters:
            self._adapter_tasks.append(asyncio.create_task(coro))
        self._pending_adapters.clear()

        snapshot_task = None
        if self._snapshots_dir:
            snapshot_task = asyncio.create_task(self._snapshot_loop())

        closure_task = None
        if self._sliding_window:
            closure_task = asyncio.create_task(self._closure_loop())

        try:
            while not (self._stop.is_set() and self.queue.empty()):
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                await self._handle(event)
        finally:
            for t in self._adapter_tasks:
                t.cancel()
            if snapshot_task is not None:
                snapshot_task.cancel()
            if closure_task is not None:
                closure_task.cancel()
            if self._snapshots_dir:
                # One last snapshot on clean shutdown so restart is exact.
                try:
                    snap_mod.snapshot(self.state, self._snapshots_dir)
                    snap_mod.prune(self._snapshots_dir)
                except Exception:
                    log.exception("final snapshot failed")

    async def _snapshot_loop(self) -> None:
        """Periodic snapshot — Design/10 §6.3."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._snapshot_interval_s)
                async with self._lock:
                    path = snap_mod.snapshot(self.state, self._snapshots_dir)
                    snap_mod.prune(self._snapshots_dir)
                log.debug("snapshot written: %s", path)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("snapshot tick failed")

    async def _closure_loop(self) -> None:
        """Periodic incident closure for sliding-window mode — Design/10 §7.4."""
        import time as _time
        from .handlers.grouping import close_stale_incidents
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._incident_closure_interval_s)
                now_ms = _time.time() * 1000.0
                async with self._lock:
                    stale = close_stale_incidents(self.state, now_ms)
                for inc_id in stale:
                    await self.submit(Event(
                        event_id=new_event_id(),
                        kind=EventKind.INCIDENT_BOUNDARY,
                        at=now_ms,
                        source="engine.closure_tick",
                        payload={"incident_id": inc_id},
                    ))
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("closure tick failed")

    async def _handle(self, event: Event) -> None:
        if event.event_id in self._seen_event_ids:
            log.debug("dedup event_id=%s kind=%s", event.event_id, event.kind)
            return
        self._seen_event_ids.add(event.event_id)

        handler = self.handlers.get(event.kind)
        if handler is None:
            log.debug("no handler for kind=%s", event.kind)
            return

        async with self._lock:
            try:
                delta = handler(event, self.state)
            except Exception:
                log.exception("handler failed for kind=%s event_id=%s",
                              event.kind, event.event_id)
                return

            for t in delta.removes:
                self.state.triples.remove(t)
            for t in delta.adds:
                self.state.triples.add(t)
            self.state.last_event_id = event.event_id

        for sub in self.subscribers:
            try:
                await sub.on_delta(delta, event)
            except Exception:
                log.exception("subscriber %r failed", sub)

    async def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Convenience entry point for the CLI.
# ---------------------------------------------------------------------------

async def run(adapters: list, subscribers: list,
              topology: Topology | None = None) -> None:
    """Spin up an engine, register adapters and subscribers, run until stop.

    `adapters` are factories: each is a callable taking the engine and
    returning a coroutine. `subscribers` are objects implementing on_delta.
    """
    engine = Engine(topology=topology)
    for sub in subscribers:
        engine.register_subscriber(sub)
    for adapter_factory in adapters:
        engine.register_adapter(adapter_factory(engine))
    await engine.run()
