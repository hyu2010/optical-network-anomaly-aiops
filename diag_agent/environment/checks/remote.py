"""Remote check primitives (doc 04 §8.3).  They read the substrate only."""

from __future__ import annotations

from ...interfaces import Substrate


class RemoteChecks:
    def __init__(self, substrate: Substrate):
        self.substrate = substrate

    def ppe_profile(self, och: str, t_s: float) -> dict:
        tr = self.substrate.ppe_trace(och, t_s)
        if tr is None:
            return {"available": False, "och": och}
        return {"available": True, **tr}

    def audit_ne(self, ne: str, t_s: float) -> list[dict]:
        return self.substrate.audit(ne=ne, t_s=t_s)

    def audit_port(self, port: str, t_s: float) -> list[dict]:
        return self.substrate.audit(port=port, t_s=t_s)
