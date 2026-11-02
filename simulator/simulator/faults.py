"""Fault catalog and propagation rules.

Each rule maps (fault-type, target) -> ordered list of `Alarm` records that a
real NMS would observe before suppression. v1 covers four faults:

  fiber_cut(span)            — primary OTS_LOS at RX side; cascades down both
                               services that traverse the cut span.
  ne_power_off(ne)           — every peer NE sees R_LOS toward this NE.
  board_power_off(ne, board) — BD_OFFLINE plus R_LOS at peers of board's ports.
  fan_failure(ne)            — FAN_FAIL plus eventual TEMP_OVER on each board.

Alarm names use the family form (no rate-suffix `k` or digit) to match the
project notes; downstream tools can normalise to the rate-specific form when
they look up the alarm catalog.
"""
from __future__ import annotations

from dataclasses import dataclass

from .topology import Topology


@dataclass
class Alarm:
    name: str
    layer: str             # OTS / OMS / OCH / OTU / ODU / client / equipment / environment
    ne: str
    board: str             # inventory board uuid (may be empty for layer-level alarms)
    port: str              # inventory port uuid (may be empty)
    container_id: str      # span / tunnel / service id this alarm is bound to
    severity: str          # Critical / Major / Minor / Warning
    t_offset_ms: int       # physical propagation time after the fault (ground truth)
    is_root: bool = False
    t_observed_ms: int | None = None
    # NMS-observed time (after per-layer poll-window jitter).  None until
    # `engine.apply_polling_delay()` derives it; downstream consumers should
    # fall back to `t_offset_ms` when this is None.
    group_id: str = ""
    # Lineage tag — which root cause this alarm descends from.  Stable across
    # the lifetime of one incident (e.g. "R1" for the first fault triggered,
    # "R2" for the second in a compound incident).  Empty string is the
    # legacy/default for single-fault incidents; the engine assigns "R1"
    # automatically when the caller doesn't supply one.


# ---------------------------------------------------------------------------
# Family-form severity defaults — used when we cannot find a rate-specific
# entry in alarm_catalog.json.  Aligned with §10 of Design/02_simulator_design.
# ---------------------------------------------------------------------------
DEFAULT_SEVERITY: dict[str, str] = {
    "OTS_LOS":       "Critical",
    "MUT_LOS":       "Critical",
    "R_LOS":         "Critical",
    "OCH_LOS":       "Critical",
    "OTU_SSF":       "Warning",
    "OTU_AIS":       "Major",
    "OTU_LOF":       "Critical",
    "OTU_LOM":       "Major",
    "OTU_BDI":       "Minor",
    "OTU_TIM":       "Major",
    "OTU_DEG":       "Minor",
    "ODU_PM_SSF":    "Minor",
    "ODU_PM_BDI":    "Minor",
    "ODU_AIS":       "Major",
    "ALS_TRIGGERED": "Critical",
    "ALS_ACTIVE":    "Minor",
    "BD_OFFLINE":    "Critical",
    "BD_STATUS":     "Critical",
    "HARD_BAD":      "Critical",
    "FAN_FAIL":      "Major",
    "TEMP_OVER":     "Major",
    "POWER_ABNORMAL":"Critical",
    "B1_EXC":        "Minor",
    "B2_EXC":        "Major",
    "OPUk_PLM":      "Major",
}


def severity_of(name: str) -> str:
    return DEFAULT_SEVERITY.get(name, "Major")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _board_of_port(t: Topology, port_uuid: str) -> str:
    p = t.ports.get(port_uuid)
    return p.board_id if p else ""


def _rx_inventory_port(t: Topology, link_network: str, ne_id: str, tp_id: str) -> str:
    tp = t._find_tp(link_network, ne_id, tp_id)
    return tp.inventory_id if tp else ""


# ---------------------------------------------------------------------------
# fiber_cut
# ---------------------------------------------------------------------------

def fiber_cut(t: Topology, span_id: str) -> list[Alarm]:
    span = t.optical_links.get(span_id)
    if not span:
        raise ValueError(f"unknown optical span: {span_id}")

    rx_ne = span.to_ne
    rx_inv = _rx_inventory_port(t, "optical-topo", rx_ne, span.to_tp)
    rx_board = _board_of_port(t, rx_inv)

    out: list[Alarm] = []

    # 1. Optical-layer primary at the RX side of the cut
    out.append(Alarm("OTS_LOS", "OTS", rx_ne, rx_board, rx_inv,
                     container_id=span_id,
                     severity=severity_of("OTS_LOS"),
                     t_offset_ms=0, is_root=True))
    out.append(Alarm("MUT_LOS", "OTS", rx_ne, rx_board, rx_inv,
                     container_id=span_id,
                     severity=severity_of("MUT_LOS"),
                     t_offset_ms=2))

    # 2. For every OTN link supported by this span, raise OCh / OTU layer alarms
    for otn_link in t.otn_links_supported_by(span_id):
        otn_rx_inv = _rx_inventory_port(
            t, "otn-topo", otn_link.to_ne, otn_link.to_tp
        )
        otn_rx_board = _board_of_port(t, otn_rx_inv)
        out.append(Alarm("R_LOS", "OCH", otn_link.to_ne, otn_rx_board, otn_rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("R_LOS"),
                         t_offset_ms=5))
        out.append(Alarm("OTU_SSF", "OTU", otn_link.to_ne, otn_rx_board, otn_rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("OTU_SSF"),
                         t_offset_ms=12))

        # BDI is reported BACK to the upstream NE
        otn_tx_inv = _rx_inventory_port(
            t, "otn-topo", otn_link.from_ne, otn_link.from_tp
        )
        otn_tx_board = _board_of_port(t, otn_tx_inv)
        out.append(Alarm("OTU_BDI", "OTU", otn_link.from_ne, otn_tx_board, otn_tx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("OTU_BDI"),
                         t_offset_ms=18))

    # 3. For every ODU tunnel that uses an affected OTN link, raise ODU_PM_SSF
    #    at the destination's client TP.
    affected_otn = {l.link_id for l in t.otn_links_supported_by(span_id)}
    for tnl in t.otn_tunnels.values():
        if not (set(tnl.ero) & affected_otn):
            continue
        client_inv = _rx_inventory_port(
            t, "otn-topo", tnl.dst_ne, tnl.dst_tp_id
        )
        client_board = _board_of_port(t, client_inv)
        out.append(Alarm("ODU_PM_SSF", "ODU", tnl.dst_ne, client_board, client_inv,
                         container_id=tnl.name,
                         severity=severity_of("ODU_PM_SSF"),
                         t_offset_ms=25))
    return out


# ---------------------------------------------------------------------------
# ne_power_off
# ---------------------------------------------------------------------------

def ne_power_off(t: Topology, ne_id: str) -> list[Alarm]:
    if ne_id not in t.nes:
        raise ValueError(f"unknown NE: {ne_id}")
    out: list[Alarm] = []

    # Mark a synthetic equipment-level root cause at the NE
    out.append(Alarm("POWER_ABNORMAL", "equipment", ne_id, "", "",
                     container_id=ne_id,
                     severity=severity_of("POWER_ABNORMAL"),
                     t_offset_ms=0, is_root=True))

    # Every link FROM the dead NE goes dark; the peer NE sees R_LOS at its RX side
    for link in t.optical_links.values():
        if link.from_ne != ne_id:
            continue
        rx_inv = _rx_inventory_port(t, "optical-topo", link.to_ne, link.to_tp)
        rx_board = _board_of_port(t, rx_inv)
        out.append(Alarm("OTS_LOS", "OTS", link.to_ne, rx_board, rx_inv,
                         container_id=link.link_id,
                         severity=severity_of("OTS_LOS"),
                         t_offset_ms=10))
        out.append(Alarm("MUT_LOS", "OTS", link.to_ne, rx_board, rx_inv,
                         container_id=link.link_id,
                         severity=severity_of("MUT_LOS"),
                         t_offset_ms=12))

    # OTN-layer cascade for any OTU pipe from the dead NE
    for otn_link in t.otn_links.values():
        if otn_link.from_ne != ne_id:
            continue
        rx_inv = _rx_inventory_port(t, "otn-topo", otn_link.to_ne, otn_link.to_tp)
        rx_board = _board_of_port(t, rx_inv)
        out.append(Alarm("R_LOS", "OCH", otn_link.to_ne, rx_board, rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("R_LOS"),
                         t_offset_ms=14))
        out.append(Alarm("OTU_SSF", "OTU", otn_link.to_ne, rx_board, rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("OTU_SSF"),
                         t_offset_ms=20))

    # ODU PM SSF at every destination client port for ODUs originating at the dead NE
    for tnl in t.otn_tunnels.values():
        if tnl.src_ne != ne_id:
            continue
        client_inv = _rx_inventory_port(t, "otn-topo", tnl.dst_ne, tnl.dst_tp_id)
        client_board = _board_of_port(t, client_inv)
        out.append(Alarm("ODU_PM_SSF", "ODU", tnl.dst_ne, client_board, client_inv,
                         container_id=tnl.name,
                         severity=severity_of("ODU_PM_SSF"),
                         t_offset_ms=30))
    return out


# ---------------------------------------------------------------------------
# board_power_off
# ---------------------------------------------------------------------------

def board_power_off(t: Topology, ne_id: str, board_id: str) -> list[Alarm]:
    ne = t.nes.get(ne_id)
    board = ne.board(board_id) if ne else None
    if not board:
        raise ValueError(f"unknown board: {ne_id}.{board_id}")

    out: list[Alarm] = []
    out.append(Alarm("BD_OFFLINE", "equipment", ne_id, board.uuid, "",
                     container_id=board.uuid,
                     severity=severity_of("BD_OFFLINE"),
                     t_offset_ms=0, is_root=True))

    # For every port on this board, find any link whose from_tp is the inventory id
    board_port_uuids = {p.uuid for p in board.ports}
    for net in ("optical-topo", "otn-topo"):
        links = t.optical_links if net == "optical-topo" else t.otn_links
        for link in links.values():
            if link.from_ne != ne_id:
                continue
            tx_inv = _rx_inventory_port(t, net, link.from_ne, link.from_tp)
            if tx_inv not in board_port_uuids:
                continue
            rx_inv = _rx_inventory_port(t, net, link.to_ne, link.to_tp)
            rx_board = _board_of_port(t, rx_inv)
            out.append(Alarm(
                "R_LOS",
                "OTS" if net == "optical-topo" else "OCH",
                link.to_ne, rx_board, rx_inv,
                container_id=link.link_id,
                severity=severity_of("R_LOS"),
                t_offset_ms=8,
            ))
    return out


# ---------------------------------------------------------------------------
# fan_failure
# ---------------------------------------------------------------------------

def fan_failure(t: Topology, ne_id: str) -> list[Alarm]:
    ne = t.nes.get(ne_id)
    if not ne:
        raise ValueError(f"unknown NE: {ne_id}")
    out: list[Alarm] = []
    fan = next((b for b in ne.boards if b.type == "FAN"), None)
    if fan:
        out.append(Alarm("FAN_FAIL", "environment", ne_id, fan.uuid, "",
                         container_id=fan.uuid,
                         severity=severity_of("FAN_FAIL"),
                         t_offset_ms=0, is_root=True))
    # Cascading TEMP_OVER on every other board (delayed)
    for b in ne.boards:
        if b.type == "FAN":
            continue
        out.append(Alarm("TEMP_OVER", "environment", ne_id, b.uuid, "",
                         container_id=b.uuid,
                         severity=severity_of("TEMP_OVER"),
                         t_offset_ms=120000))
    return out


# ---------------------------------------------------------------------------
# fiber_attenuation_high  (soft fault: dirty connector / aged fiber)
# ---------------------------------------------------------------------------

def fiber_attenuation_high(t: Topology, span_id: str) -> list[Alarm]:
    span = t.optical_links.get(span_id)
    if not span:
        raise ValueError(f"unknown optical span: {span_id}")
    rx_inv = _rx_inventory_port(t, "optical-topo", span.to_ne, span.to_tp)
    rx_board = _board_of_port(t, rx_inv)

    out: list[Alarm] = [
        Alarm("IN_PWR_LOW", "OTS", span.to_ne, rx_board, rx_inv,
              container_id=span_id,
              severity=severity_of("IN_PWR_LOW"),
              t_offset_ms=0, is_root=True),
    ]
    # OTU-layer pre-correction degradation appears at every supported OTU pipe
    for otn_link in t.otn_links_supported_by(span_id):
        otn_rx_inv = _rx_inventory_port(
            t, "otn-topo", otn_link.to_ne, otn_link.to_tp
        )
        otn_rx_board = _board_of_port(t, otn_rx_inv)
        out.append(Alarm("BEFFEC_EXC", "OTU", otn_link.to_ne, otn_rx_board, otn_rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("BEFFEC_EXC"),
                         t_offset_ms=8))
        out.append(Alarm("OTU_DEG", "OTU", otn_link.to_ne, otn_rx_board, otn_rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of("OTU_DEG"),
                         t_offset_ms=12))
    return out


# ---------------------------------------------------------------------------
# connector_dirty  (sub-threshold PPE-first demo fault — Design/12 §1.3)
# ---------------------------------------------------------------------------
#
# Localised attenuation at one z_km point along the span — models a
# contaminated / loose fibre connector at a splice or amplifier site.
# Magnitude is small (0.5–3.0 dB step) so most cases don't trip OTU
# thresholds at all; the alarms-only system is blind.  Only stronger
# contamination (≥ 1.5 dB) raises an intermittent Minor-severity
# BEFFEC_EXC, and even that is NOT flagged is_root — the diagnostic signal
# lives in PPE (a sharp step at the connector z_km), not in alarms.
#
# This is the killer-demo fault for PPE: alarms-only RCA misses every
# low-magnitude case; PPE-enabled RCA catches all of them, with z_km
# localisation.  See Design/12 §1.3 and §9 (R5 rule).

def connector_dirty(t: Topology, span_id: str,
                    z_km: float, magnitude_db: float) -> list[Alarm]:
    span = t.optical_links.get(span_id)
    if not span:
        raise ValueError(f"unknown optical span: {span_id}")

    # Sub-threshold magnitudes produce zero alarms — alarms-only system is
    # blind, PPE is the only signal.
    if magnitude_db < 1.5:
        return []

    # Above-threshold magnitudes may produce one Minor-severity BEFFEC_EXC
    # at the first downstream OTU receiver. NOT marked is_root: BEFFEC_EXC
    # is a symptom; the diagnostic signal is the PPE step. This makes
    # connector_dirty the only fault in the catalog whose incidents may have
    # an empty alarm list or no is_root alarm — by design.
    for otn_link in t.otn_links_supported_by(span_id):
        rx_inv = _rx_inventory_port(
            t, "otn-topo", otn_link.to_ne, otn_link.to_tp
        )
        rx_board = _board_of_port(t, rx_inv)
        return [
            Alarm("BEFFEC_EXC", "OTU", otn_link.to_ne, rx_board, rx_inv,
                  container_id=otn_link.link_id,
                  severity="Minor",
                  t_offset_ms=30, is_root=False),
        ]
    return []


# ---------------------------------------------------------------------------
# Bit-error / FEC family — three separate fault ids per the design decision
# ---------------------------------------------------------------------------

def _bit_error_seed(t: Topology, span_id: str, alarm_name: str,
                    layer: str, secondary_alarms: tuple[str, ...] = ()) -> list[Alarm]:
    span = t.optical_links.get(span_id)
    if not span:
        raise ValueError(f"unknown optical span: {span_id}")
    out: list[Alarm] = []
    is_root = True
    for otn_link in t.otn_links_supported_by(span_id):
        otn_rx_inv = _rx_inventory_port(
            t, "otn-topo", otn_link.to_ne, otn_link.to_tp
        )
        otn_rx_board = _board_of_port(t, otn_rx_inv)
        out.append(Alarm(alarm_name, layer, otn_link.to_ne, otn_rx_board, otn_rx_inv,
                         container_id=otn_link.link_id,
                         severity=severity_of(alarm_name),
                         t_offset_ms=0, is_root=is_root))
        is_root = False
        for i, sec in enumerate(secondary_alarms):
            out.append(Alarm(sec, layer if not sec.startswith(("OTU", "ODU")) else "OTU",
                             otn_link.to_ne, otn_rx_board, otn_rx_inv,
                             container_id=otn_link.link_id,
                             severity=severity_of(sec),
                             t_offset_ms=10 + 5*i))
    return out


def b1_exc_high(t: Topology, span_id: str) -> list[Alarm]:
    return _bit_error_seed(t, span_id, "B1_EXC", layer="SDH",
                           secondary_alarms=("OTU_DEG",))


def b2_exc_high(t: Topology, span_id: str) -> list[Alarm]:
    # B2 suppresses B1 in the SDH chain; we name only B2 here, L1 forward-walk
    # adds the rest.
    return _bit_error_seed(t, span_id, "B2_EXC", layer="SDH",
                           secondary_alarms=("OTU_DEG",))


def fec_pre_cor_high(t: Topology, span_id: str) -> list[Alarm]:
    return _bit_error_seed(t, span_id, "BEFFEC_EXC", layer="OTU",
                           secondary_alarms=("FEC_BEF_COR_ER", "OTU_DEG"))


# ---------------------------------------------------------------------------
# rx_power_drift  ("light signal drift")
# ---------------------------------------------------------------------------

def rx_power_drift(t: Topology, ne_id: str, board_id: str = "",
                   port_id: str = "", direction: str = "low") -> list[Alarm]:
    """Optical input power drifts out of spec on a single line port.

    direction = 'low'  -> IN_PWR_LOW  + IN_PWR_ABN
    direction = 'high' -> IN_PWR_HIGH + IN_PWR_ABN
    If port_id is empty we pick the first FIU input port we can find on the NE.
    """
    ne = t.nes.get(ne_id)
    if not ne:
        raise ValueError(f"unknown NE: {ne_id}")
    # Resolve a sensible port if the caller didn't pin one
    if not port_id:
        for b in ne.boards:
            if b.type != "FIU":
                continue
            for p in b.ports:
                if "from_" in p.name or "in" in p.name:
                    port_id = p.uuid
                    board_id = b.uuid
                    break
            if port_id:
                break
    if not port_id:
        raise ValueError(f"no input port found on {ne_id}")
    primary = "IN_PWR_LOW" if direction == "low" else "IN_PWR_HIGH"
    return [
        Alarm(primary, "OTS", ne_id, board_id, port_id,
              container_id=port_id,
              severity=severity_of(primary),
              t_offset_ms=0, is_root=True),
        Alarm("IN_PWR_ABN", "OTS", ne_id, board_id, port_id,
              container_id=port_id,
              severity=severity_of("IN_PWR_ABN"),
              t_offset_ms=3),
    ]


# ---------------------------------------------------------------------------
# config_mismatch  (wavelength / rate / trail-id)
# ---------------------------------------------------------------------------

def config_mismatch(t: Topology, ne_id: str, board_id: str = "",
                    port_id: str = "") -> list[Alarm]:
    """Mismatch detected on an OTU receive port: any of the OPU/OTU/ODU TIM
    family fires depending on which header was misconfigured.  We emit the
    ones an operator is most likely to see together.
    """
    ne = t.nes.get(ne_id)
    if not ne:
        raise ValueError(f"unknown NE: {ne_id}")
    if not port_id:
        for b in ne.boards:
            if b.type != "OTU":
                continue
            for p in b.ports:
                if "rx" in p.name or "from_" in p.name:
                    port_id = p.uuid
                    board_id = b.uuid
                    break
            if port_id:
                break
    if not port_id:
        raise ValueError(f"no OTU rx port found on {ne_id}")
    return [
        Alarm("OPU_PLM", "ODU", ne_id, board_id, port_id,
              container_id=port_id,
              severity=severity_of("OPU_PLM"),
              t_offset_ms=0, is_root=True),
        Alarm("OTU_TIM", "OTU", ne_id, board_id, port_id,
              container_id=port_id,
              severity=severity_of("OTU_TIM"),
              t_offset_ms=4),
        Alarm("ODU_PM_TIM", "ODU", ne_id, board_id, port_id,
              container_id=port_id,
              severity=severity_of("ODU_PM_TIM"),
              t_offset_ms=8),
    ]


# ---------------------------------------------------------------------------
# Catalog — (rule, target_kind, L2 keyword set)
# ---------------------------------------------------------------------------

CATALOG: dict[str, tuple] = {
    "fiber_cut":               (fiber_cut,               "link",  {"fiber", "los"}),
    "fiber_attenuation_high":  (fiber_attenuation_high,  "link",  {"fiber", "optical_power"}),
    "connector_dirty":         (connector_dirty,         "link_z", set()),
    "b1_exc_high":             (b1_exc_high,             "link",  {"biterr"}),
    "b2_exc_high":             (b2_exc_high,             "link",  {"biterr"}),
    "fec_pre_cor_high":        (fec_pre_cor_high,        "link",  {"biterr"}),
    "rx_power_drift":          (rx_power_drift,          "port",  {"optical_power"}),
    "ne_power_off":            (ne_power_off,            "ne",    {"power_supply"}),
    "board_power_off":         (board_power_off,         "board", {"power_supply"}),
    "fan_failure":             (fan_failure,             "ne",    {"temp"}),
    "config_mismatch":         (config_mismatch,         "port",  {"config"}),
}


def trigger(t: Topology, fault_type: str, *args) -> list[Alarm]:
    """Return only the *seed* alarms for the fault.  L1+L2 expansion lives in
    `engine.expand_cascade()`."""
    if fault_type not in CATALOG:
        raise ValueError(f"unknown fault: {fault_type}")
    fn, _kind, _kw = CATALOG[fault_type]
    return fn(t, *args)


def keywords_for(fault_type: str) -> set[str]:
    return CATALOG[fault_type][2]
