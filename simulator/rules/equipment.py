"""Design/08 §5 — equipment & environment "trump cards".

NE- and board-level events override every other alarm at the same location
because they cause it. These rules are deterministic (no weights).

  POWER_ABNORMAL @ NE n  -> every alarm at the NE is Secondary
  BD_OFFLINE @ board b   -> every alarm at the board is Secondary
  FAN_FAIL + TEMP_OVER   -> TEMP_OVER is Secondary to FAN_FAIL
  HARD_BAD @ board b     -> every alarm at the board is Secondary
"""
from __future__ import annotations

from .schema import Inferred

TRUMP_ALARMS_NE_LEVEL = {"POWER_ABNORMAL"}
TRUMP_ALARMS_BOARD_LEVEL = {"BD_OFFLINE", "HARD_BAD"}


def apply(alarms: list[dict]) -> list[Inferred]:
    out: list[Inferred] = []
    for r in alarms:
        nm = r["alarm_name"]
        if nm in TRUMP_ALARMS_NE_LEVEL:
            for o in alarms:
                if o["id"] == r["id"]:
                    continue
                if o.get("ne") == r.get("ne"):
                    out.append(Inferred("Secondary", o["id"], r["id"],
                                        source=f"§5.{nm.lower()}"))
        elif nm in TRUMP_ALARMS_BOARD_LEVEL:
            for o in alarms:
                if o["id"] == r["id"]:
                    continue
                if o.get("board") and o.get("board") == r.get("board"):
                    out.append(Inferred("Secondary", o["id"], r["id"],
                                        source=f"§5.{nm.lower()}"))
        elif nm == "FAN_FAIL":
            for o in alarms:
                if o["id"] == r["id"]:
                    continue
                if o["alarm_name"] == "TEMP_OVER" and o.get("ne") == r.get("ne"):
                    out.append(Inferred("Secondary", o["id"], r["id"],
                                        source="§5.fan_fail"))
    return out
