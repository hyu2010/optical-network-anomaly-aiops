"""Field checks (doc 04 §8.3).

In replay mode the `FieldOracle` plays the engineer at the fibre.  Apart from
the scorer it is the only component allowed to read labels, and it answers
only the question asked, within the check's tolerance.  In live mode a field
check parks the graph at `interrupt()` and a human answers.
"""

from __future__ import annotations

import random


class FieldOracle:
    def __init__(self, labels, seed: int = 0):
        self.labels = labels
        self.rng = random.Random(seed)

    def answer(self, check_id: str, target: str) -> dict:
        truth = self.labels.truth()
        ft, args = truth["fault_type"], truth["args"]
        tgt0 = args[0] if args else ""
        tgt1 = args[1] if len(args) > 1 else ""
        if check_id == "CHK_OTDR":
            if target != tgt0 or ft not in ("fiber_cut", "fiber_attenuation_high", "connector_dirty"):
                return {"event": "none"}
            pos = self.labels.root_ppe_position(target)
            if pos is None and ft == "connector_dirty" and len(args) > 1:
                pos = float(args[1])
            out = {"event": {"fiber_cut": "break", "fiber_attenuation_high": "loss",
                             "connector_dirty": "reflective"}[ft]}
            if pos is not None:
                out["position_km"] = round(pos + self.rng.uniform(-0.05, 0.05), 2)
            return out
        if check_id == "CHK_CONNECTOR_INSPECT":
            return {"connector": "dirty" if (ft == "connector_dirty" and target == tgt0) else "clean"}
        if check_id == "CHK_BOARD_RESEAT":
            hit = (ft == "board_power_off" and target == tgt1)
            return {"board": "recovers" if hit else "no-change"}
        if check_id == "CHK_LOOPBACK":
            hit = ft in ("rx_power_drift", "config_mismatch") and target in args
            return {"fault": "inside" if hit else "outside"}
        if check_id == "CHK_FAN_INSPECT":
            return {"fan": "failed" if (ft == "fan_failure" and target == tgt0) else "ok"}
        return {"error": f"no field procedure for {check_id}"}


class HumanFieldDesk:
    """Live mode: the answer arrives through `diag-agent answer` (graph resume)."""

    def answer(self, check_id: str, target: str) -> dict:
        raise RuntimeError("field checks in live mode are answered through interrupt()/resume")
