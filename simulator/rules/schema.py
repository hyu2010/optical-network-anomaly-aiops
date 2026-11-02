"""Dataclasses the reasoner passes around.

Rules consume a list of `Alarm` dicts (one per row in `alarms` table) plus the
`Topology` object, and emit `Inferred` records — `Secondary(a, parent)`,
`RootCandidate(a, fault_type)`, or `RootCause(a, fault_type)`.

Each inferred record carries a `source` tag (which rule fired) and a
confidence weight, so the orchestrator can rank conflicting picks.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Inferred:
    kind: str               # "Secondary" | "RootCandidate" | "RootCause"
    alarm_id: int           # the alarm being labelled (subject)
    parent_id: int = 0      # for Secondary: the alarm it's caused by; for Root*: same as alarm_id
    fault_type: str = ""    # for RootCandidate / RootCause: which §8 fingerprint matched
    source: str = ""        # the rule that fired (e.g. "§3.layer_cascade", "§8.1.fiber_cut")
    weight: float = 1.0


@dataclass
class Inference:
    """The reasoner's verdict on one incident."""
    incident_id: int
    root_alarm_id: int = 0          # picked root (0 if reasoner couldn't decide)
    root_alarm_name: str = ""
    fault_type: str = ""             # predicted fault class (empty if unknown)
    source: str = ""                 # which rule picked the root
    inferred: list[Inferred] = field(default_factory=list)
    # For analysis:
    n_candidates: int = 0
    n_secondaries: int = 0
