"""Grounding: run a chosen check against the environment (doc 04 §8.3).

Remote checks read the substrate (PPE profile, alarm audits); CHK_WAIT just
lets agent time pass; field checks go to the `FieldAnswerer` (the oracle in
replay, a human through interrupt()/resume in live mode).
"""

from __future__ import annotations

from typing import Any

from ...environment.checks.remote import RemoteChecks


class CheckDispatcher:
    """Concrete `CheckExecutor`."""

    def __init__(self, sem, substrate, field=None):
        self.sem = sem
        self.remote = RemoteChecks(substrate)
        self.field = field

    def duration_s(self, check_id: str, target: str, t_s: float, until: float | None = None) -> float:
        if check_id == "CHK_WAIT":
            return max(0.0, (until or t_s) - t_s)
        return self.sem.checks()[check_id]["minutes"] * 60.0

    def audit_port_for(self, check_id: str, target: str) -> str | None:
        if check_id == "CHK_REVERSE_FIBRE":
            rev = self.sem.value(target, "reverseOf")
            return self.sem.value(rev, "rxPort") if rev else None
        if check_id == "CHK_SERVICE_STATE":
            return self.sem.value(target, "clientPort")
        if check_id == "CHK_TRAIL_TRACE_AUDIT":
            return target
        return None

    def run(self, check_id: str, target: str, t_s: float, until: float | None = None) -> dict[str, Any]:
        t_done = t_s + self.duration_s(check_id, target, t_s, until)
        obs: dict[str, Any] = {"kind": "check", "check_id": check_id, "target": target, "t_start": round(t_s, 3),
                               "t_done": round(t_done, 3), "remote": self.sem.checks()[check_id]["remote"]}
        if check_id == "CHK_PPE_PROFILE":
            r = self.remote.ppe_profile(target, t_done)
            obs["trace"] = r if r.get("available") else None
            obs["answer"] = {"available": bool(r.get("available")),
                             "anomalies": r.get("anomalies", []) if r.get("available") else []}
        elif check_id == "CHK_ALARM_AUDIT":
            alarms = self.remote.audit_ne(target, t_done)
            obs["audit"] = {"ne": target, "t": t_done}
            obs["alarms"] = alarms
            obs["answer"] = {"alarms": sorted({a["name"] for a in alarms})}
        elif check_id in ("CHK_REVERSE_FIBRE", "CHK_SERVICE_STATE", "CHK_TRAIL_TRACE_AUDIT"):
            port = self.audit_port_for(check_id, target)
            alarms = self.remote.audit_port(port, t_done) if port else []
            obs["audit"] = {"port": port, "t": t_done}
            obs["alarms"] = alarms
            obs["answer"] = {"port": port, "alarms": sorted({a["name"] for a in alarms})}
        elif check_id == "CHK_WAIT":
            obs["answer"] = {"waited_s": round(t_done - t_s, 3)}
        else:
            if self.field is None:
                obs["answer"] = {"error": "no field desk configured"}
            else:
                obs["answer"] = self.field.answer(check_id, target)
            obs["field"] = True
        return obs
