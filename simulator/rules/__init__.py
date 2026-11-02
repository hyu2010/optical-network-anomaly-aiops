"""Rule-based RCA reasoner over the simulator's alarm cascades.

Implements the rule families in `Design/08_root_cause_rules.md`:
  §3 layer-cascade   - layer_cascade.py
  §4 topology-grounded cascade - topology_cascade.py
  §5 equipment trump cards - equipment.py
  §8 fault-class fingerprints - fingerprints.py

The orchestrator in `reasoner.py` runs the rules in priority order and returns
one predicted root cause per incident.  `evaluate.py` scores predictions
against the simulator's `is_root` ground truth and emits a per-fault accuracy
table plus a confusion matrix.
"""
from .reasoner import infer_root, Inference

__all__ = ["infer_root", "Inference"]
