"""Rule-engine subscriber — Design/10 §8.

Listens for `alarm.raise` events; for each, re-runs `src.rules.infer_root`
against the current alarms in the affected incident, and submits a follow-up
event to the engine so the picked root is materialised as a triple with
`source = "rule:fingerprint"`. As more alarms arrive the pick can flip; we
emit a remove of the old pick and an add of the new one.

This lives outside the normal Delta path because the rule engine needs
*read* access to the full incident state (which only exists after the
alarm.raise handler runs). We push its output back through the engine via a
synthetic `rule.root_pick` event so it goes through the same locking,
broadcast, and event-log machinery as everything else.
"""
from __future__ import annotations

import logging
import time

from simulator.kg.schema import ROOT_CAUSE, Triple, alarm_uri
from simulator.rules import infer_root

from ..events import (
    Delta, Event, EventKind, SOURCE_RULE_FINGERPRINT, new_event_id,
)
from ..state import StateStore

log = logging.getLogger(__name__)

RULE_ROOT_PICK = "rule.root_pick"   # synthetic event kind


class RuleEngineSubscriber:
    """Re-evaluate the rule engine on each new alarm and emit RootCause picks.

    Parameters
    ----------
    engine : the Engine instance — the subscriber pushes synthetic events back.
    min_alarms : skip evaluation until the incident has this many alarms
                 (default 1; bump to e.g. 3 if the rule engine is too noisy
                 on tiny incidents).
    """

    def __init__(self, engine, *, min_alarms: int = 1) -> None:
        self.engine = engine
        self.min_alarms = min_alarms
        # incident_id -> alarm_id currently picked as root
        self._picks: dict[int, int] = {}

    async def on_delta(self, delta, event):
        if event.kind != EventKind.ALARM_RAISE:
            return
        # The alarm-raise handler stamped this row onto an incident; find it.
        row = event.payload.get("row") or {}
        incident_id = event.payload.get("incident_id")
        if incident_id is None:
            # Sliding-window mode: the incident_id was assigned by the grouper.
            # Look it up by finding which incident now contains this row's id.
            row_id = row.get("id")
            for iid, inc in self.engine.state.incidents.items():
                if any(r.get("id") == row_id for r in inc.alarm_rows):
                    incident_id = iid
                    break
        if incident_id is None:
            return

        inc = self.engine.state.incidents.get(incident_id)
        if inc is None or len(inc.alarm_rows) < self.min_alarms:
            return

        try:
            inf = infer_root(incident_id, inc.alarm_rows, self.engine.state.topology)
        except Exception:
            log.exception("rule engine failed for incident %s", incident_id)
            return
        new_pick = inf.root_alarm_id or 0
        old_pick = self._picks.get(incident_id, 0)
        if new_pick == old_pick:
            return

        self._picks[incident_id] = new_pick
        await self.engine.submit(Event(
            event_id=new_event_id(),
            kind=RULE_ROOT_PICK,
            at=time.time() * 1000.0,
            source="rule_engine_subscriber",
            payload={
                "incident_id": incident_id,
                "old_pick":    old_pick,
                "new_pick":    new_pick,
                "fault_type":  inf.fault_type,
                "fingerprint": inf.source,
            },
        ))


def handle_rule_root_pick(event: Event, state: StateStore) -> Delta:
    """Apply the rule-engine pick swap as a Delta — Design §16 Q5.

    Removes the previous fingerprint-sourced RootCause triple for this
    incident (if any) and adds the new one.
    """
    incident_id = event.payload["incident_id"]
    old_pick    = event.payload.get("old_pick") or 0
    new_pick    = event.payload.get("new_pick") or 0

    delta = Delta()
    if old_pick:
        old_uri = alarm_uri(incident_id, old_pick)
        delta.removes.append(Triple(old_uri, ROOT_CAUSE, old_uri,
                                     SOURCE_RULE_FINGERPRINT, 1.0, incident_id))
    if new_pick:
        new_uri = alarm_uri(incident_id, new_pick)
        delta.adds.append(Triple(new_uri, ROOT_CAUSE, new_uri,
                                  SOURCE_RULE_FINGERPRINT, 1.0, incident_id))
    return delta
