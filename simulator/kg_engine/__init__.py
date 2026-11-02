"""Real-time knowledge-graph engine — streaming counterpart to `src/kg/`.

Replaces the batch builder (`src/kg/builder.py`) with a streaming, stateful
service that consumes topology changes and alarm events incrementally.

See `Design/10_realtime_kg_engine.md` for the full architecture.

R-1 scope: parity with the batch builder via streaming. The engine consumes
one incident at a time from `data/incidents.db` and emits the same triples
that `src/kg/builder.py::build_kg` would produce (minus correlation R1-R4
edges and topology triples, which are R-3 and one-shot respectively).
"""
from .engine import Engine, run

__all__ = ["Engine", "run"]
