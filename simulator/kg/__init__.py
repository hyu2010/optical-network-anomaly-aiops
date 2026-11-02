"""Knowledge-graph builder for the OSN fault simulator.

Public entrypoint: `build_kg(...)`. See `Design/09_knowledge_graph_builder.md`
for the schema and `python -m simulator.kg --help` for the CLI.
"""
from .builder import build_kg

__all__ = ["build_kg"]
