"""Flask server for the Network Fault Simulator GUI.

Wraps the existing simulator backend (yang_loader / faults / engine / db).
Run with:    python -m simulator.app.server
Open with:   http://127.0.0.1:5000/
"""
from __future__ import annotations

import csv
import functools
import io
import json
import os
import random
import sqlite3
import zipfile
from datetime import datetime, timezone
from typing import Any

import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from simulator.simulator import db, engine, faults, format_alm, ppe, ppe_plot, yang_loader

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Hand-laid coordinates per topology generation.  v3 = lab mesh (6 hubs A..F);
# v2 (diamond+crossbar 4 NEs ne1..ne4) lives in data/yang/v2/ and is kept here
# as a fallback for back-compat with v2 incidents.db.
HUB_POS_V3 = {
    "A": (200,  60),
    "B": (480,  60),
    "C": (200, 280),
    "D": (480, 280),
    "E": (200, 480),
    "F": (480, 480),
}
DIAMOND_POS_V2 = {
    "ne1": (260,  40), "ne2": ( 80, 220), "ne4": (440, 220), "ne3": (260, 400),
}

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
TOPOLOGY = yang_loader.load()


# ---------------------------------------------------------------------------
# /  +  static
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> Response:
    return send_from_directory(STATIC_DIR, "index.html")


# ---------------------------------------------------------------------------
# /api/topology — vis-network-friendly nodes + edges
# ---------------------------------------------------------------------------

@app.route("/api/topology")
def api_topology() -> Response:
    # Resolve coordinates against whichever generation's hand-laid map matches
    # the loaded NE id set. Falls back to (0, 0) which lets vis-network pick.
    pos_map = HUB_POS_V3 if any(ne in HUB_POS_V3 for ne in TOPOLOGY.nes) \
        else DIAMOND_POS_V2
    nodes = []
    for ne_id in TOPOLOGY.nes:
        x, y = pos_map.get(ne_id, (0, 0))
        nodes.append({
            "id": ne_id,
            "label": ne_id,
            "x": x, "y": y,
            "fixed": False,
            "group": "ne",
        })
    edges = []
    for span in TOPOLOGY.optical_links.values():
        # Mark edges that carry a service (so we can colour them in the UI)
        services = [
            t.client_service_name
            for t in TOPOLOGY.otn_tunnels.values()
            for otn_link_id in t.ero
            if TOPOLOGY.otn_links.get(otn_link_id) and
               TOPOLOGY.otn_links[otn_link_id].supporting_link_id == span.link_id
        ]
        edges.append({
            "id":   span.link_id,
            "from": span.from_ne,
            "to":   span.to_ne,
            "label": span.link_id,
            "arrows": "to",
            "services": sorted(set(services)),
        })
    return jsonify({"nodes": nodes, "edges": edges,
                    "topology_id": TOPOLOGY.topology_id()})


# ---------------------------------------------------------------------------
# /api/faults — fault catalog + valid targets per fault
# ---------------------------------------------------------------------------

@app.route("/api/faults")
def api_faults() -> Response:
    fault_meta = []
    for fault_id, (_fn, kind, kw) in faults.CATALOG.items():
        fault_meta.append({
            "id": fault_id,
            "label": _humanise(fault_id),
            "target_kind": kind,
            "keywords": sorted(kw),
        })
    # connector_dirty (target_kind "link_z") only produces signal on spans that
    # actually carry an OCh channel — mirror generate_dataset.all_targets_for so
    # the UI doesn't offer dead spans.  Ship each span's length so the UI can
    # default z_km to mid-span and bound the input.
    channel_carrying_spans = {span_id for tn in TOPOLOGY.wdm_tunnels.values()
                              for span_id in tn.ero}
    targets = {
        "link":  sorted(TOPOLOGY.optical_links.keys()),
        "link_z": [
            {"id": span_id,
             "length_km": float(TOPOLOGY.optical_links[span_id].length_km)}
            for span_id in sorted(TOPOLOGY.optical_links)
            if span_id in channel_carrying_spans
            and TOPOLOGY.optical_links[span_id].length_km > 0
        ],
        "ne":    sorted(TOPOLOGY.nes.keys()),
        "board": [
            [ne_id, b.uuid]
            for ne_id, ne in sorted(TOPOLOGY.nes.items())
            for b in ne.boards
            if b.type in ("OTU", "OA", "FIU", "SCC", "FAN")
        ],
        "port": sorted(TOPOLOGY.nes.keys()),
    }
    return jsonify({"faults": fault_meta, "targets": targets})


def _humanise(fault_id: str) -> str:
    return fault_id.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# /api/trigger — run a fault, persist, return alarm cascade
# ---------------------------------------------------------------------------

@app.route("/api/trigger", methods=["POST"])
def api_trigger() -> Response:
    result = _run_trigger(request.json or {})
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


def _run_trigger(body: dict) -> dict:
    """Single-fault trigger.  Always tags the cascade with group_id='R1' so
    the response shape is symmetric with /api/trigger_multi.  Generates
    paired PPE traces and records them on both raw + suppressed incidents
    so the UI can plot the per-channel power profile alongside alarms."""
    fault_type = body.get("fault_type", "")
    args = body.get("args", []) or []
    if fault_type not in faults.CATALOG:
        return {"error": f"unknown fault: {fault_type}"}
    fn, target_kind, kw = faults.CATALOG[fault_type]
    try:
        seeds = fn(TOPOLOGY, *args)
    except (TypeError, ValueError) as exc:
        return {"error": f"fault rule failed: {exc}"}
    raw = engine.expand_cascade(seeds, kw, group_id="R1")
    raw = engine.apply_polling_delay(raw)
    suppressed = engine.apply_suppression(raw)

    # PPE side — paired with the same fault args.  Failure here is
    # non-fatal: the UI still gets alarms, just no PPE panel content.
    ppe_traces: list = []
    try:
        ppe_rng = random.Random()  # system-clock seeded; persisted blobs make
                                   # replay from history deterministic anyway
        ppe_traces = engine.expand_ppe(TOPOLOGY, fault_type, *args,
                                       rng=ppe_rng, group_id="R1")
        ppe_traces = ppe.apply_polling_delay(ppe_traces, ppe_rng)
    except (TypeError, ValueError) as exc:
        app.logger.warning("PPE generation failed for %s %s: %s",
                           fault_type, args, exc)

    conn = db.open_db()
    target_str = " ".join(str(a) for a in args) or "(none)"
    # One shared wall-clock base for both twins of this trigger, so the alarms'
    # calendar time (base + t_observed_ms) is consistent across the response,
    # the DB, and the NMS dump.
    base_dt = datetime.now(timezone.utc)
    base_iso = base_dt.isoformat(timespec="seconds")
    base_ms = int(base_dt.timestamp() * 1000)
    sid_raw = db.record(
        conn, fault_type=fault_type, fault_target=target_str,
        target_kind=target_kind, suppressed=False,
        topology_id=TOPOLOGY.topology_id(), alarms=raw,
        ppe_traces=ppe_traces, note="GUI trigger (raw)", created_at=base_iso,
    )
    sid_sup = db.record(
        conn, fault_type=fault_type, fault_target=target_str,
        target_kind=target_kind, suppressed=True,
        topology_id=TOPOLOGY.topology_id(), alarms=suppressed,
        ppe_traces=ppe_traces, note="GUI trigger (suppressed)", created_at=base_iso,
    )
    conn.close()
    return {
        "incident_id_raw":        sid_raw,
        "incident_id_suppressed": sid_sup,
        "fault_type":             fault_type,
        "fault_target":           target_str,
        "target_kind":            target_kind,
        "raw":        [_alarm_dict(a, base_ms) for a in raw],
        "suppressed": [_alarm_dict(a, base_ms) for a in suppressed],
        "groups":     [{"id": "R1", "fault_type": fault_type,
                        "fault_target": target_str}],
        "ppe_trace_count":        len(ppe_traces),
    }


# ---------------------------------------------------------------------------
# /api/trigger_multi — fire two or more faults nearly simultaneously and tag
# every emitted alarm with R1 / R2 / … so derived alarms can be grouped by
# which root they descend from.
# ---------------------------------------------------------------------------

@app.route("/api/trigger_multi", methods=["POST"])
def api_trigger_multi() -> Response:
    body = request.json or {}
    fault_specs = body.get("faults", []) or []
    delay_ms = int(body.get("delay_ms", 0))   # offset applied to R2..RN's t_offset_ms

    if not fault_specs or len(fault_specs) < 2:
        return jsonify({"error": "trigger_multi needs >=2 faults"}), 400

    all_raw: list[faults.Alarm] = []
    all_ppe: list = []
    groups_meta: list[dict] = []

    for i, spec in enumerate(fault_specs, start=1):
        ft = spec.get("fault_type", "")
        ar = spec.get("args", []) or []
        gid = f"R{i}"
        if ft not in faults.CATALOG:
            return jsonify({"error": f"unknown fault: {ft}"}), 400
        fn, kind, kw = faults.CATALOG[ft]
        try:
            seeds = fn(TOPOLOGY, *ar)
        except (TypeError, ValueError) as exc:
            return jsonify({"error": f"fault rule {ft} failed: {exc}"}), 400
        cascade = engine.expand_cascade(seeds, kw, group_id=gid)
        # Stagger R2..RN so their physical times don't completely overlap R1's.
        if i > 1 and delay_ms > 0:
            for a in cascade:
                a.t_offset_ms += delay_ms * (i - 1)
        all_raw.extend(cascade)
        # PPE per fault — failure on any one fault is non-fatal; we just
        # skip its traces.
        try:
            traces = engine.expand_ppe(TOPOLOGY, ft, *ar,
                                       rng=random.Random(), group_id=gid)
            if i > 1 and delay_ms > 0:
                for tr in traces:
                    tr.t_offset_ms += delay_ms * (i - 1)
            all_ppe.extend(traces)
        except (TypeError, ValueError) as exc:
            app.logger.warning("PPE generation failed for %s %s: %s",
                               ft, ar, exc)
        groups_meta.append({
            "id": gid,
            "fault_type": ft,
            "fault_target": " ".join(str(x) for x in ar) or "(none)",
            "target_kind": kind,
        })

    raw = engine.apply_polling_delay(all_raw)
    suppressed = engine.apply_suppression(raw)
    all_ppe = ppe.apply_polling_delay(all_ppe) if all_ppe else []

    # One incident row per view; fault_type / fault_target encode the compound.
    combined_type   = "+".join(g["fault_type"]   for g in groups_meta)
    combined_target = " | ".join(f"{g['id']}:{g['fault_target']}" for g in groups_meta)

    conn = db.open_db()
    base_dt = datetime.now(timezone.utc)
    base_iso = base_dt.isoformat(timespec="seconds")
    base_ms = int(base_dt.timestamp() * 1000)
    sid_raw = db.record(
        conn, fault_type=combined_type, fault_target=combined_target,
        target_kind="multi", suppressed=False,
        topology_id=TOPOLOGY.topology_id(), alarms=raw,
        ppe_traces=all_ppe,
        note=f"compound trigger ({len(groups_meta)} roots, raw)",
        created_at=base_iso,
    )
    sid_sup = db.record(
        conn, fault_type=combined_type, fault_target=combined_target,
        target_kind="multi", suppressed=True,
        topology_id=TOPOLOGY.topology_id(), alarms=suppressed,
        ppe_traces=all_ppe,
        note=f"compound trigger ({len(groups_meta)} roots, suppressed)",
        created_at=base_iso,
    )
    conn.close()

    return jsonify({
        "incident_id_raw":        sid_raw,
        "incident_id_suppressed": sid_sup,
        "fault_type":             combined_type,
        "fault_target":           combined_target,
        "target_kind":            "multi",
        "groups":                 groups_meta,
        "raw":        [_alarm_dict(a, base_ms) for a in raw],
        "suppressed": [_alarm_dict(a, base_ms) for a in suppressed],
        "ppe_trace_count":        len(all_ppe),
    })


def _alarm_dict(a: faults.Alarm, base_ms: int | None = None) -> dict:
    rel = a.t_observed_ms if a.t_observed_ms is not None else a.t_offset_ms
    observed_at_ms = (base_ms + int(rel or 0)) if base_ms is not None else None
    return {
        "name":           a.name,
        "layer":          a.layer,
        "ne":             a.ne,
        "board":          a.board,
        "port":           a.port,
        "container_id":   a.container_id,
        "severity":       a.severity,
        "t_offset_ms":    a.t_offset_ms,
        "t_observed_ms":  rel,
        # Absolute calendar time the operator sees (incident base + relative
        # offset) — the time handle the diag/PPE chain uses to pin the snapshot.
        "observed_at_ms": observed_at_ms,
        "observed_at":    (datetime.fromtimestamp(observed_at_ms / 1000.0,
                                                  tz=timezone.utc).isoformat()
                           if observed_at_ms is not None else None),
        "is_root":        a.is_root,
        "group_id":       a.group_id or "R1",
        "description":    _description_for(a.name),
    }


# ---------------------------------------------------------------------------
# Alarm description lookup — returns the human-readable line from
# `data/alarm_catalog.json`.  Family-form names (`OTU_SSF`, `ODU_PM_SSF`)
# are resolved by trying each rate variant.
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _alarm_catalog_9800() -> dict:
    """v3 — primary alarm catalog from the OSN 9800 product documentation."""
    p = os.path.join(ROOT, "data", "alarm_catalog_9800.json")
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


@functools.lru_cache(maxsize=1)
def _alarm_catalog_8800() -> dict:
    """v2 fallback catalog — covers alarms 9800 may not document."""
    p = os.path.join(ROOT, "data", "alarm_catalog.json")
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


_RATE_VARIANTS = ("k", "0", "1", "2", "2e", "3", "4", "Cn", "5G")
_RATE_FAMILIES = ("OTU", "ODU", "OPU")


def _lookup(cat: dict, name: str) -> str:
    if not cat:
        return ""
    # 1. direct hit
    if name in cat:
        return cat[name].get("description", "") or ""
    # 2. OCh_ in the suppression file vs OCH_ in the catalog
    if name.startswith("OCh_"):
        cand = "OCH_" + name[4:]
        if cand in cat:
            return cat[cand].get("description", "") or ""
    # 3. Rate-specific variants for OTU/ODU/OPU family forms
    for prefix in _RATE_FAMILIES:
        head = prefix + "_"
        if name.startswith(head):
            tail = name[len(head):]
            for rate in _RATE_VARIANTS:
                cand = f"{prefix}{rate}_{tail}"
                if cand in cat:
                    return cat[cand].get("description", "") or ""
    return ""


@functools.lru_cache(maxsize=2000)
def _description_for(name: str) -> str:
    """Prefer the 9800 description; fall back to 8800 if 9800 doesn't have it."""
    desc = _lookup(_alarm_catalog_9800(), name)
    if desc:
        return desc
    return _lookup(_alarm_catalog_8800(), name)


# ---------------------------------------------------------------------------
# /api/yang/<name> — raw access to the three YANG instance files
# /api/services    — digested service summary (composed from the loaded
#                    Topology object so the UI doesn't re-parse tunnels.json)
# ---------------------------------------------------------------------------

_YANG_FILES = {
    "topology":  "topology.json",
    "tunnels":   "tunnels.json",
    "inventory": "inventory.json",
}


@app.route("/api/yang/<name>")
def api_yang_file(name: str) -> Response:
    if name not in _YANG_FILES:
        return jsonify({"error": f"unknown YANG file: {name}"}), 404
    path = os.path.join(ROOT, "data", "yang", _YANG_FILES[name])
    if not os.path.exists(path):
        return jsonify({"error": f"file not found: {path}"}), 404
    with open(path, "r", encoding="utf-8") as f:
        body = f.read()
    return Response(body, mimetype="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/services")
def api_services() -> Response:
    """Per-service summary table — one row per ODU/OCh pair (== one service).

    Joins each OTN tunnel (the 'client service' layer with the
    nfms:client-service-name tag) with its supporting OCh tunnel and the
    optical spans on the OCh's ERO.  Also detects shared spans for the
    R4 demo annotation (ch2 and ch4 both traverse span_OMS3_BC in v3).
    """
    # First pass: count how many OChs use each span — non-1 counts are R4 candidates.
    span_users: dict[str, list[str]] = {}
    for wdm in TOPOLOGY.wdm_tunnels.values():
        for span in wdm.ero:
            span_users.setdefault(span, []).append(wdm.name)

    services: list[dict] = []
    for ot in TOPOLOGY.otn_tunnels.values():
        wdm = TOPOLOGY.wdm_tunnels.get(ot.supporting_tunnel)
        ero = list(wdm.ero) if wdm else []
        total_km = 0.0
        spans_info = []
        for span_id in ero:
            link = TOPOLOGY.optical_links.get(span_id)
            length = float(link.length_km) if link else 0.0
            total_km += length
            shared_with = [n for n in span_users.get(span_id, [])
                           if n != (wdm.name if wdm else "")]
            spans_info.append({
                "id": span_id,
                "length_km": length,
                "shared_with": shared_with,
            })
        services.append({
            "service_name": ot.client_service_name or ot.name,
            "odu_tunnel":   ot.name,
            "och_tunnel":   ot.supporting_tunnel,
            "source":       ot.src_ne,
            "destination":  ot.dst_ne,
            "tx_power_dbm": float(wdm.tx_channel_power_dbm) if wdm else None,
            "spans":        spans_info,
            "n_spans":      len(ero),
            "total_km":     round(total_km, 1),
            "rate_type":    ot.rate_type,
        })
    services.sort(key=lambda s: s["service_name"])
    return jsonify({
        "n_services":  len(services),
        "topology_id": TOPOLOGY.topology_id(),
        "services":    services,
    })


# ---------------------------------------------------------------------------
# /api/incidents — recent incidents for the history panel
# ---------------------------------------------------------------------------

@app.route("/api/incidents")
def api_incidents() -> Response:
    limit = int(request.args.get("limit", 20))
    conn = db.open_db()
    rows = conn.execute(
        """SELECT id, created_at, fault_type, fault_target, target_kind,
                  suppressed,
                  (SELECT count(*) FROM alarms WHERE incident_id=incidents.id),
                  (SELECT count(*) FROM ppe_traces WHERE incident_id=incidents.id)
           FROM incidents ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify([
        {"id": r[0], "created_at": r[1], "fault_type": r[2],
         "fault_target": r[3], "target_kind": r[4],
         "suppressed": bool(r[5]), "alarm_count": r[6],
         "ppe_trace_count": r[7]}
        for r in rows
    ])


@app.route("/api/incidents/<int:incident_id>")
def api_incident_detail(incident_id: int) -> Response:
    conn = db.open_db()
    head = conn.execute(
        "SELECT id, fault_type, fault_target, suppressed FROM incidents WHERE id=?",
        (incident_id,),
    ).fetchone()
    if not head:
        conn.close()
        return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        """SELECT id, alarm_name, ne, board, port, severity, layer,
                  container_id, t_offset_ms, t_observed_ms
           FROM alarms WHERE incident_id=? ORDER BY t_observed_ms""",
        (incident_id,),
    ).fetchall()
    conn.close()
    # incidents.db carries no root-cause ground truth (prompt 12): the persisted
    # alarm read-back is a raw NMS-style dump.  Re-derive the root alarm on the
    # fly with the SAME rule reasoner the KG builder uses
    # (simulator.rules.infer_root, Design/08 rules), so the UI can still flag the
    # root (★) consistently when viewing a recorded incident — without storing
    # any answer key in the DB.  A reasoner failure must not 500 the endpoint.
    root_id = None
    try:
        from simulator.rules import infer_root
        reasoner_rows = [
            {"id": r[0], "alarm_name": r[1], "ne": r[2], "board": r[3],
             "port": r[4], "layer": r[6], "container_id": r[7],
             "t_offset_ms": r[8]}
            for r in rows
        ]
        root_id = infer_root(incident_id, reasoner_rows, TOPOLOGY).root_alarm_id
    except Exception as exc:  # noqa: BLE001 -- defensive: keep the panel working
        app.logger.warning("root inference failed for incident %s: %s",
                            incident_id, exc)
    return jsonify({
        "id": head[0], "fault_type": head[1], "fault_target": head[2],
        "suppressed": bool(head[3]),
        "alarms": [
            {"name": r[1], "ne": r[2], "board": r[3], "port": r[4],
             "severity": r[5], "layer": r[6], "container_id": r[7],
             "t_offset_ms": r[8],
             "t_observed_ms": r[9] if r[9] is not None else r[8],
             "is_root": (r[0] == root_id),
             "group_id": "R1",
             "description": _description_for(r[1])}
            for r in rows
        ],
    })


# ---------------------------------------------------------------------------
# /api/incidents/<id>/ppe.png  +  /api/incidents/<id>/ppe.json
# Reconstruct PpeTrace objects from ppe_traces + ppe_anomalies rows and
# render a matplotlib PNG of the per-(channel, span) power profile.
# ---------------------------------------------------------------------------

def _load_ppe_traces(conn: sqlite3.Connection, incident_id: int) -> list[ppe.PpeTrace]:
    rows = conn.execute(
        """SELECT id, och, drop_ne, drop_board, total_length_km, group_id,
                  t_offset_ms, t_observed_ms, delta_z_m, n_samples,
                  samples_blob, baseline_blob, degraded
           FROM ppe_traces WHERE incident_id=? ORDER BY id""",
        (incident_id,),
    ).fetchall()
    # Reconstruct each OCh's span_ids / z_offsets from the live topology so the
    # plot can draw OMS boundaries (these aren't stored per-trace in the DB).
    topo = TOPOLOGY
    traces: list[ppe.PpeTrace] = []
    for r in rows:
        och = r[1]
        tn = topo.wdm_tunnels.get(och)
        span_ids, z_offsets, off = [], [], 0.0
        if tn is not None:
            for sid in tn.ero:
                lk = topo.optical_links.get(sid)
                if lk is None or lk.length_km <= 0:
                    continue
                span_ids.append(sid)
                z_offsets.append(off)
                off += lk.length_km
        tr = ppe.PpeTrace(
            och=och,
            drop_ne=r[2], drop_board=r[3] or "",
            total_length_km=float(r[4] or 0.0),
            z_offsets=z_offsets, span_ids=span_ids,
            group_id=r[5] or "",
            t_offset_ms=int(r[6] or 0),
            t_observed_ms=r[7],
            delta_z_m=int(r[8]),
            samples=np.frombuffer(r[10], dtype=db.PPE_BLOB_DTYPE).copy(),
            baseline=np.frombuffer(r[11], dtype=db.PPE_BLOB_DTYPE).copy(),
            degraded=bool(r[12]),
        )
        anom_rows = conn.execute(
            """SELECT kind, z_km, z_end_km, span_id, offset_in_span_km,
                      magnitude_db, confidence, is_root
               FROM ppe_anomalies WHERE trace_id=? ORDER BY id""",
            (r[0],),
        ).fetchall()
        for a in anom_rows:
            tr.anomalies.append(ppe.PpeAnomaly(
                kind=a[0], z_km=a[1], z_end_km=a[2],
                span_id=a[3] or "", offset_in_span_km=a[4],
                magnitude_db=a[5] if a[5] is not None else 0.0,
                confidence=a[6] if a[6] is not None else 0.0,
                is_root=bool(a[7]),
            ))
        traces.append(tr)
    return traces


def _incident_title(conn: sqlite3.Connection, incident_id: int) -> str:
    row = conn.execute(
        "SELECT fault_type, fault_target, suppressed FROM incidents WHERE id=?",
        (incident_id,),
    ).fetchone()
    if not row:
        return f"incident #{incident_id}"
    view = "suppressed" if row[2] else "raw"
    return f"#{incident_id} {row[0]} on {row[1]} ({view})"


@app.route("/api/incidents/<int:incident_id>/ppe.png")
def api_incident_ppe_png(incident_id: int) -> Response:
    conn = db.open_db()
    try:
        traces = _load_ppe_traces(conn, incident_id)
        title = _incident_title(conn, incident_id)
    finally:
        conn.close()
    png = ppe_plot.render_png(traces, title)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/incidents/<int:incident_id>/ppe.json")
def api_incident_ppe_json(incident_id: int) -> Response:
    conn = db.open_db()
    try:
        traces = _load_ppe_traces(conn, incident_id)
    finally:
        conn.close()
    return jsonify({
        "incident_id": incident_id,
        "n_traces":    len(traces),
        "traces": [
            {
                "och":             tr.och,
                "drop_ne":         tr.drop_ne,
                "drop_board":      tr.drop_board,
                "total_length_km": tr.total_length_km,
                "span_ids":        tr.span_ids,
                "z_offsets":       tr.z_offsets,
                "group_id":        tr.group_id,
                "delta_z_m":       tr.delta_z_m,
                "degraded":        tr.degraded,
                "t_observed_ms":   tr.t_observed_ms,
                "samples":  [None if np.isnan(s) else float(s)
                             for s in tr.samples],
                "baseline": [float(b) for b in tr.baseline],
                "anomalies": [
                    {"kind": a.kind, "z_km": a.z_km, "z_end_km": a.z_end_km,
                     "span_id": a.span_id,
                     "offset_in_span_km": a.offset_in_span_km,
                     "magnitude_db": a.magnitude_db, "confidence": a.confidence,
                     "is_root": a.is_root}
                    for a in tr.anomalies
                ],
            }
            for tr in traces
        ],
    })


# ---------------------------------------------------------------------------
# /api/incidents/<id>/dump.txt — render the cascade in Huawei NMS format
# ---------------------------------------------------------------------------

@app.route("/api/incidents/<int:incident_id>/dump.txt")
def api_incident_dump(incident_id: int) -> Response:
    conn = db.open_db()
    try:
        text = format_alm.format_incident_from_db(conn, incident_id)
    finally:
        conn.close()
    if text is None:
        return Response(f"incident {incident_id} not found\n",
                        status=404, mimetype="text/plain")
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="incident_{incident_id}_dump.txt"'},
    )


# ---------------------------------------------------------------------------
# /api/clear — wipe the DB
# ---------------------------------------------------------------------------

@app.route("/api/clear", methods=["POST"])
def api_clear() -> Response:
    conn = db.open_db()
    conn.execute("DELETE FROM ppe_anomalies")
    conn.execute("DELETE FROM ppe_traces")
    conn.execute("DELETE FROM alarms")
    conn.execute("DELETE FROM incidents")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# /api/export — return incidents.csv + alarms.csv as a ZIP
# ---------------------------------------------------------------------------

@app.route("/api/export")
def api_export() -> Response:
    conn = db.open_db()
    incidents = conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
    sce_cols = [d[0] for d in conn.execute("SELECT * FROM incidents LIMIT 0").description]
    alarms = conn.execute("SELECT * FROM alarms ORDER BY id").fetchall()
    al_cols = [d[0] for d in conn.execute("SELECT * FROM alarms LIMIT 0").description]
    conn.close()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("incidents.csv", _to_csv(sce_cols, incidents))
        z.writestr("alarms.csv",    _to_csv(al_cols,  alarms))
        # Ship the input artefacts the ML pipeline needs alongside the dataset.
        for f in ("alarm_catalog.json", "alarm_suppression.json"):
            p = os.path.join(ROOT, "data", f)
            if os.path.exists(p):
                with open(p, "rb") as src:
                    z.writestr(f, src.read())
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": 'attachment; filename="incidents_export.zip"'},
    )


def _to_csv(cols: list[str], rows: list[tuple]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    w.writerows(rows)
    return out.getvalue()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
