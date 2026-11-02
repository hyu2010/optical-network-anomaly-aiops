"""Minimal SSE demo — `python -m simulator.kg_engine web` runs an embedded Flask
server that hosts the engine and streams its delta feed to any browser.

Routes:

  GET /                       a single-page live event viewer (HTML + JS)
  GET /api/kg-engine/events   SSE stream (one `data: {...}` per event)
  GET /api/kg-engine/state    point-in-time JSON snapshot of triple counts

Architecture:

  Main thread     - Flask WSGI server (debug-mode)
  Engine thread   - runs the asyncio engine loop forever
  SSE generator   - per-request iterator pulling from a queue.Queue that
                    the SSESubscriber fan-out fills

Integration with the existing `src/app/server.py` is left as a follow-up:
mount this module's `_build_blueprint(...)` onto the existing Flask app and
the live stream is available at the same origin as the UI.
"""
from __future__ import annotations

import asyncio
import os
import threading

from flask import Blueprint, Flask, Response, jsonify, render_template_string

from .adapters import SimulatorAdapter, YangWatchAdapter
from .engine import Engine
from .subscribers import (CsvMirrorSubscriber, EventLogSubscriber,
                          RuleEngineSubscriber, SSESubscriber)


def _build_blueprint(engine: Engine, sse: SSESubscriber) -> Blueprint:
    bp = Blueprint("kg_engine", __name__, url_prefix="/api/kg-engine")

    @bp.route("/events")
    def events():
        def stream():
            yield from sse.subscribe()
        return Response(stream(), mimetype="text/event-stream")

    @bp.route("/state")
    def state():
        from collections import Counter
        # The engine thread can mutate `all_triples` while we iterate;
        # retry on the inevitable "set changed size during iteration" race.
        for _ in range(5):
            try:
                snap = list(engine.state.triples.all_triples)
                break
            except RuntimeError:
                continue
        else:
            return jsonify({"error": "state changing too fast, retry"}), 503
        return jsonify({
            "triples":          len(snap),
            "incidents":        len(engine.state.incidents),
            "topology_version": engine.state.topology_version,
            "last_event_id":    engine.state.last_event_id,
            "by_source":        dict(Counter(t.source for t in snap)),
        })

    return bp


_PAGE = """\
<!doctype html>
<title>KG Engine Live</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 1em; }
  pre  { background: #f4f4f4; padding: 0.5em; overflow-x: auto; }
  .e   { border-left: 3px solid #2a7; padding-left: 0.5em; margin: 0.5em 0;
         font-family: monospace; font-size: 12px; }
  .stats { padding: 0.5em; background: #efefef; border-radius: 4px; }
</style>
<h1>KG Engine — Live Event Stream</h1>
<div class=stats id=stats>connecting...</div>
<div id=feed></div>
<script>
  function fmtTriple(t) {
    return `${t[0]}  ${t[1]}  ${t[2]}   (src=${t[3]})`;
  }
  async function refreshStats() {
    const r = await fetch("/api/kg-engine/state");
    const j = await r.json();
    document.getElementById("stats").innerText =
      `triples: ${j.triples}   incidents: ${j.incidents}   ` +
      `topology_version: ${j.topology_version}   ` +
      `by source: ${JSON.stringify(j.by_source)}`;
  }
  setInterval(refreshStats, 2000); refreshStats();

  const es = new EventSource("/api/kg-engine/events");
  const feed = document.getElementById("feed");
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    const div = document.createElement("div");
    div.className = "e";
    const adds    = m.delta.adds.length;
    const removes = m.delta.removes.length;
    const preview = m.delta.adds.slice(0, 2).map(fmtTriple).join("\\n");
    div.innerText =
      `${new Date(m.event.at).toISOString()}  ${m.event.kind}  ` +
      `(+${adds}/-${removes})\\n${preview}`;
    feed.insertBefore(div, feed.firstChild);
    if (feed.children.length > 100) feed.removeChild(feed.lastChild);
  };
</script>
"""


def create_app(db_path: str | None = None,
               *, mirror_csv: bool = True,
               kg_dir: str | None = None) -> Flask:
    """Build the demo Flask app with an engine running in a background thread.

    Parameters
    ----------
    db_path : optional path to incidents.db (default data/incidents.db)
    mirror_csv : when True (default), register a CsvMirrorSubscriber so each
        new fault click in the simulator GUI appends to data/kg/*.csv in real
        time. Topology edits under data/yang/ also rewrite topology_triples.csv.
    kg_dir : output directory for the mirror (default data/kg)
    """
    app = Flask(__name__)
    engine = Engine()
    sse = SSESubscriber()
    engine.register_subscriber(sse)
    engine.register_subscriber(EventLogSubscriber(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "data", "kg_engine"), fsync=False))
    engine.register_subscriber(RuleEngineSubscriber(engine))
    if mirror_csv:
        if kg_dir is None:
            kg_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "..", "data", "kg")
        engine.register_subscriber(CsvMirrorSubscriber(engine.state,
                                                       kg_dir=kg_dir))
    # YangWatchAdapter — emits initial topology.snapshot at startup, then
    # re-emits whenever any data/yang/*.json file changes (mtime poll).
    engine.register_adapter(YangWatchAdapter()(engine))
    # start_from_now so the UI doesn't have to wait for a full-DB drain
    # before showing live updates.
    engine.register_adapter(SimulatorAdapter(db_path=db_path,
                                              start_from_now=True)(engine))

    # Run the engine in its own thread with its own asyncio loop.
    def _engine_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(engine.run())
        finally:
            loop.close()

    t = threading.Thread(target=_engine_thread, daemon=True, name="kg_engine")
    t.start()

    app.register_blueprint(_build_blueprint(engine, sse))

    @app.route("/")
    def index():
        return render_template_string(_PAGE)

    return app


def main(host: str = "127.0.0.1", port: int = 5001,
         db_path: str | None = None,
         *, mirror_csv: bool = True,
         kg_dir: str | None = None) -> None:
    create_app(db_path, mirror_csv=mirror_csv, kg_dir=kg_dir).run(
        host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
