"""Render a simulator alarm cascade in the Huawei NMS `ALM-CUR-DATA-ALL`
text format used by the lab dump in `Library/Alm_format.txt`.

Used by:
- `src/app/server.py::api_incident_dump` — serves `/api/incidents/<id>/dump.txt`.
- the CLI: `python -m simulator.simulator.format_alm <incident_id>` reads
  `data/incidents.db` and prints the dump to stdout.

Mapping decisions (deviations from a real Huawei dump):
- Severity codes: Critical/Major/Minor/Warning -> CR/MJ/MN/WR.
- SRVAFF: Critical|Major -> SA, otherwise NSA.
- STATE: always 'start' — we don't model alarm-clear events yet.
- NUM: synthetic, autoincrementing from `start_num` (default 400000).
- PARA: `board=<short>,subcard=255,port=<short>,group=1;;;<placeholder hex>` —
  uses our simulator's logical board/port suffixes since we don't model
  Huawei slot numbers.
- The trailing CLI prompt uses NE A's IP unless overridden.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import DEFAULT_DB

# ---------------------------------------------------------------------------

_SEV = {"Critical": "CR", "Major": "MJ", "Minor": "MN", "Warning": "WR"}
_SA  = {"Critical": "SA", "Major": "SA", "Minor": "NSA", "Warning": "NSA"}

# Lab management IPs (per Design/07_lab_mesh_topology.md) — used only to make
# the trailing CLI prompt look plausible.
_NE_IP = {
    "A": "70.31.65.95",  "B": "70.31.65.96",  "C": "70.31.65.133",
    "D": "70.31.65.130", "E": "70.31.65.91",  "F": "70.31.65.101",
}
_FALLBACK_IP = "70.31.65.95"

_PARA_HEX = "00 ff 00 0000 00 00 ff ff ;"

_HEADER = (
    f"  {'NUM':<10}  {'EID':<36}  {'SEVERITY':<10}  {'STATE':<10}  "
    f"{'START-TIME':<29}  {'END-TIME':<29}  {'SRVAFF':<6}  "
    f"{'OBJ-TYPE':<33}  PARA"
)


def _obj_type(name: str) -> str:
    if name in ("FAN_FAIL",):
        return "FAN"
    if name in ("BD_OFFLINE", "HARD_BAD", "BD_STATUS",
                "POWER_ABNORMAL", "TEMP_OVER"):
        return "BOARD"
    if name.startswith((
        "OTS_", "MUT_", "OMS_", "OCH_", "OCh_",
        "R_LOS", "IN_PWR", "OUT_PWR", "CHAN_LOS",
        "OTU", "ODU", "OPU", "B1_", "B2_", "HP_",
        "BEFFEC", "FEC_", "ALS", "LSR", "LASER",
    )):
        return "LASER_GROUP"
    return "BOARD"


def _para(board: str, port: str, ne: str = "") -> str:
    bd = (board or "").split(".", 1)[-1] or "-"
    pt = (port  or "").split(".", 1)[-1] or "-"
    # `ne=<NE>` is non-Huawei but identifies which Network Element raised the
    # alarm — the raw dump otherwise only carries the board/port suffix, which
    # is ambiguous across NEs (every NE has a `FIU.line_from_B` etc.).
    ne_pfx = f"ne={ne}," if ne else ""
    return f"{ne_pfx}board={bd},subcard=255,port={pt};;;{_PARA_HEX}"


def _row(num: int, alarm: dict, ts: str) -> str:
    sev = _SEV.get(alarm["severity"], "WR")
    sa  = _SA.get(alarm["severity"], "NSA")
    return (
        f"  {num:<10}  {alarm['name']:<36}  {sev:<10}  "
        f"{'start':<10}  {ts:<29}  {'None':<29}  "
        f"{sa:<6}  {_obj_type(alarm['name']):<33}  "
        f"{_para(alarm.get('board',''), alarm.get('port',''), alarm.get('ne',''))}"
    )


def format_alarms(
    alarms: Iterable[dict],
    *,
    base_time: datetime,
    start_num: int = 400000,
    cli_ip: str = _FALLBACK_IP,
    cli_user: str = "ORC_02",
) -> str:
    """Render an iterable of alarm dicts as an `ALM-CUR-DATA-ALL` dump.

    Each alarm dict needs: name, severity, board, port, t_offset_ms.
    Timezone-naive `base_time` is treated as UTC.
    """
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)

    def _t(a):
        # Prefer the NMS-observed time (post poll-jitter) when present,
        # else fall back to the physical propagation time.
        v = a.get("t_observed_ms")
        return v if v is not None else a.get("t_offset_ms", 0)

    alarm_list = sorted(alarms, key=_t)

    out = ["ALM-CUR-DATA-ALL", _HEADER]
    for i, a in enumerate(alarm_list, start=start_num):
        ts = (base_time + timedelta(milliseconds=int(_t(a)))) \
            .strftime("%Y-%m-%d %H:%M:%S+00:00")
        out.append(_row(i, a, ts))
    out.append(f"  Total records :{len(alarm_list)}")
    out.append("")
    last_offset = _t(alarm_list[-1]) if alarm_list else 0
    prompt_ts = (base_time + timedelta(milliseconds=int(last_offset) + 50)) \
        .strftime("%Y-%m-%d %H:%M:%S+00:00")
    out.append(f"#9-1:{cli_user} [{cli_ip}][Unspecified][{prompt_ts}]>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# DB-driven entry: read one incident from incidents.db and render it.
# ---------------------------------------------------------------------------

def format_incident_from_db(conn: sqlite3.Connection, incident_id: int) -> str | None:
    head = conn.execute(
        "SELECT id, created_at, fault_type, fault_target, target_kind, suppressed "
        "FROM incidents WHERE id=?", (incident_id,),
    ).fetchone()
    if not head:
        return None
    rows = conn.execute(
        "SELECT alarm_name, ne, board, port, severity, layer, "
        "       container_id, t_offset_ms, t_observed_ms "
        "FROM alarms WHERE incident_id=? ORDER BY t_observed_ms", (incident_id,),
    ).fetchall()
    # incidents.db has no root-cause ground truth (prompt 12): the raw NMS-style
    # dump shows no is_root marker.  Root inference is a downstream rule step.
    alarms = [
        dict(name=r[0], ne=r[1], board=r[2], port=r[3], severity=r[4],
             layer=r[5], container_id=r[6],
             t_offset_ms=r[7],
             t_observed_ms=r[8] if r[8] is not None else r[7],
             is_root=False,
             group_id="R1")
        for r in rows
    ]
    try:
        base = datetime.fromisoformat(head[1])
    except ValueError:
        base = datetime.now(timezone.utc)
    cli_ip = _NE_IP.get(alarms[0]["ne"], _FALLBACK_IP) if alarms else _FALLBACK_IP
    # Salt the start_num with the incident id so two consecutive incidents
    # don't both emit alarms 400000..400066.
    start_num = 400000 + incident_id * 1000
    return format_alarms(alarms, base_time=base, start_num=start_num, cli_ip=cli_ip)


# ---------------------------------------------------------------------------
# CLI: python -m simulator.simulator.format_alm <incident_id> [--db PATH]
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Render a incident from incidents.db in Huawei NMS dump format."
    )
    p.add_argument("incident_id", type=int, help="incident id from incidents.db")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"path to incidents.db (default: {DEFAULT_DB})")
    args = p.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        text = format_incident_from_db(conn, args.incident_id)
    finally:
        conn.close()
    if text is None:
        print(f"incident id {args.incident_id} not found in {args.db}",
              file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
