"""Orchestrator — runs the rule families in priority order and picks a root.

Pipeline per incident:

  1. Run §8 fingerprint rules.  Each matches a specific fault class to a
     candidate root alarm.  When several match, the highest-weighted one
     wins; ties broken by fingerprint priority order.
  2. Run §3 layer-cascade + §4 topology-cascade + §5 equipment rules to mark
     every other alarm as Secondary(a, root) — informational, not used for
     root pick.

Important: §5's POWER_ABNORMAL / BD_OFFLINE / FAN_FAIL / HARD_BAD alarms are
NOT trusted as root by name alone.  The simulator's L2 keyword expansion
routinely pulls POWER_ABNORMAL into fiber_attenuation_high / rx_power_drift
cascades (matching the "input power" keyword), so trump-by-name produces
massive false positives.  The §8.5–§8.7 fingerprints carry the additional
guards (e.g. "ForAll spans from this NE have OTS_LOS") that confirm the
equipment alarm is truly the root.

Fallback (no fingerprint fires): pick the alarm at the lowest layer
(OTS < OMS < OCH < OTU < SDH/ODU < client) that nothing else marks as
Secondary.  Graceful degradation, NOT recommended — Design/08 §6 cautions
that polling-delay jitter makes "earliest timestamp = root" unsafe.
"""
from __future__ import annotations

from simulator.simulator.topology import Topology

from . import equipment, fingerprints, layer_cascade, topology_cascade
from .schema import Inference, Inferred

LAYER_RANK = {
    "OTS":    0, "OMS":    1, "OCH":    2, "OTU":    3,
    "SDH":    4, "FEC":    4, "ODU":    4, "OPU":    4,
    "client": 5, "equipment": 0, "environment": 0,
}


def infer_root(incident_id: int, alarms: list[dict],
               topo: Topology) -> Inference:
    """Return one root-cause prediction for the incident."""
    inf = Inference(incident_id=incident_id)
    by_id = {a["id"]: a for a in alarms}

    # § 8 — fault-class fingerprints (the primary root picker).
    candidates = fingerprints.apply(alarms, topo)
    inf.n_candidates = len(candidates)
    if candidates:
        # Highest weight wins; ties broken by fingerprint priority (= order
        # in FINGERPRINTS, which is preserved in `candidates`).
        candidates.sort(key=lambda c: -c.weight)
        pick = candidates[0]
        a = by_id[pick.alarm_id]
        inf.root_alarm_id   = pick.alarm_id
        inf.root_alarm_name = a["alarm_name"]
        inf.fault_type      = pick.fault_type
        inf.source          = pick.source
        inf.inferred.append(Inferred("RootCause", pick.alarm_id, pick.alarm_id,
                                     fault_type=pick.fault_type,
                                     source=pick.source))
    inf.inferred.extend(candidates)
    inf.inferred.extend(layer_cascade.apply(alarms))
    inf.inferred.extend(topology_cascade.apply(alarms, topo))
    inf.inferred.extend(equipment.apply(alarms))
    inf.n_secondaries = sum(1 for x in inf.inferred if x.kind == "Secondary")

    # Fallback if no fingerprint matched: lowest-layer alarm not labelled
    # Secondary by anything.
    if not inf.root_alarm_id:
        secondary_ids = {x.alarm_id for x in inf.inferred if x.kind == "Secondary"}
        candidates_fb = [a for a in alarms if a["id"] not in secondary_ids]
        if not candidates_fb:
            candidates_fb = alarms
        candidates_fb.sort(key=lambda a: (LAYER_RANK.get(a.get("layer") or "", 99),
                                          a.get("t_offset_ms") or 0))
        fb = candidates_fb[0]
        inf.root_alarm_id   = fb["id"]
        inf.root_alarm_name = fb["alarm_name"]
        inf.fault_type      = ""
        inf.source          = "fallback.lowest_layer"
        inf.inferred.append(Inferred("RootCause", fb["id"], fb["id"],
                                     source=inf.source))

    return inf


