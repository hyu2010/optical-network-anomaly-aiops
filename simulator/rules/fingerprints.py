"""Design/08 §8 — fault-class fingerprints.

Each of the 10 ★ priority faults has a characteristic alarm constellation.
A fingerprint identifies *the* alarm that should be the root cause of the
incident AND assigns the fault class. Fingerprints with more positive
premises and more NOT clauses are more specific — when several match, the
most specific one wins.

Output: list of `RootCandidate` records, one per fingerprint match.
"""
from __future__ import annotations

from simulator.simulator.topology import Topology

from .schema import Inferred


def _names_at_span(alarms: list[dict], span: str) -> set[str]:
    return {a["alarm_name"] for a in alarms if a.get("container_id") == span}


def _names_at_ne(alarms: list[dict], ne: str) -> set[str]:
    return {a["alarm_name"] for a in alarms if a.get("ne") == ne}


def _names_at_port(alarms: list[dict], port: str) -> set[str]:
    return {a["alarm_name"] for a in alarms if a.get("port") == port}


def _names_at_container(alarms: list[dict], container: str) -> set[str]:
    return {a["alarm_name"] for a in alarms if a.get("container_id") == container}


def _names_at_board(alarms: list[dict], board: str) -> set[str]:
    return {a["alarm_name"] for a in alarms if a.get("board") == board}


def _carries(topo: Topology, otu_or_container: str) -> str:
    """If `otu_or_container` is an OTU link, return its supporting span. Else ''."""
    if otu_or_container in topo.otn_links:
        return topo.otn_links[otu_or_container].supporting_link_id
    return ""


# ---------------------------------------------------------------------------
# Each fingerprint: callable(alarms, topo) -> list[Inferred(RootCandidate)]
# ---------------------------------------------------------------------------

def fp_fiber_cut(alarms, topo):
    out = []
    for r in alarms:
        if r["alarm_name"] != "OTS_LOS":
            continue
        span = r.get("container_id") or ""
        if span not in topo.optical_links:
            continue
        names = _names_at_span(alarms, span)
        if "MUT_LOS" in names:
            out.append(Inferred("RootCandidate", r["id"], r["id"],
                                fault_type="fiber_cut",
                                source="§8.1.fiber_cut", weight=2.0))
    return out


def fp_fiber_attenuation_high(alarms, topo):
    """IN_PWR_LOW at a span + BEFFEC_EXC at a topology container (span or OTU
    pipe) at the same NE, with no OTS_LOS on the span.  OTU_DEG is in the
    raw cascade but the suppressed view drops it, so we don't require it.
    The "BEFFEC_EXC on a topology container" (not a port) is the
    discriminator that separates this from rx_power_drift's L2 noise.
    """
    out = []
    for r in alarms:
        if r["alarm_name"] != "IN_PWR_LOW":
            continue
        span = r.get("container_id") or ""
        if span not in topo.optical_links:
            continue
        if "OTS_LOS" in _names_at_span(alarms, span):
            continue
        ne = r.get("ne") or ""
        beffec_on_topo = any(
            a["alarm_name"] == "BEFFEC_EXC"
            and a.get("ne") == ne
            and (a.get("container_id") in topo.optical_links
                 or a.get("container_id") in topo.otn_links)
            for a in alarms
        )
        if beffec_on_topo:
            out.append(Inferred("RootCandidate", r["id"], r["id"],
                                fault_type="fiber_attenuation_high",
                                source="§8.2.fiber_attenuation_high",
                                weight=1.8))
    return out


def _bit_error_fp(alarms, topo, name, fault_type):
    out = []
    for r in alarms:
        if r["alarm_name"] != name or r.get("layer") != "SDH":
            continue
        # NOT Exists OTS_LOS on the same span.  The container can be either
        # the OTU pipe (resolve to supporting span) or the span itself —
        # L2 keyword expansion places derived alarms at the seed's location,
        # which is often the span directly.
        container = r.get("container_id") or ""
        if container in topo.optical_links:
            span = container
        else:
            span = _carries(topo, container)
        if span and "OTS_LOS" in _names_at_span(alarms, span):
            continue
        # Block when a stronger §8 root signature sits at the same NE — these
        # get pulled into bit-error cascades via L2 keyword expansion and
        # would otherwise turn into false b1/b2_exc predictions.
        #   • OTS_LOS, POWER_ABNORMAL, FAN_FAIL → §8.1 / §8.5 / §8.7
        #   • OPU_PLM → §8.8 config_mismatch
        # We do NOT block on BEFFEC_EXC: it co-occurs naturally in b1/b2 cascades
        # (the "biterr" keyword pulls it in), so blocking on it kills the rule.
        ne = r.get("ne") or ""
        if ne and {"OTS_LOS", "POWER_ABNORMAL", "FAN_FAIL", "OPU_PLM"} & _names_at_ne(alarms, ne):
            continue
        out.append(Inferred("RootCandidate", r["id"], r["id"],
                            fault_type=fault_type,
                            source=f"§8.3.{fault_type}", weight=1.6))
    return out


def fp_b1_exc_high(alarms, topo):
    return _bit_error_fp(alarms, topo, "B1_EXC", "b1_exc_high")


def fp_b2_exc_high(alarms, topo):
    return _bit_error_fp(alarms, topo, "B2_EXC", "b2_exc_high")


def fp_fec_pre_cor_high(alarms, topo):
    out = []
    for r in alarms:
        if r["alarm_name"] != "BEFFEC_EXC" or r.get("layer") != "OTU":
            continue
        port = r.get("port") or ""
        if not port:
            continue
        names_port = _names_at_port(alarms, port)
        if "FEC_BEF_COR_ER" not in names_port:
            continue
        # NOT Exists IN_PWR_LOW on same container (rules out fiber_attenuation_high).
        container = r.get("container_id") or ""
        if container and "IN_PWR_LOW" in _names_at_container(alarms, container):
            continue
        # Block when a stronger root signature sits at the same NE (these get
        # pulled into the cascade via L2 keyword expansion).
        ne = r.get("ne") or ""
        if ne and {"OTS_LOS", "POWER_ABNORMAL", "FAN_FAIL"} & _names_at_ne(alarms, ne):
            continue
        out.append(Inferred("RootCandidate", r["id"], r["id"],
                            fault_type="fec_pre_cor_high",
                            source="§8.3.fec_pre_cor_high", weight=1.7))
    return out


def fp_rx_power_drift(alarms, topo):
    """The simulator's `rx_power_drift` defaults to direction="low" and emits
    IN_PWR_LOW as the root (IN_PWR_HIGH only appears via L2 expansion in
    other fault classes).  We therefore emit a single candidate per incident,
    preferring IN_PWR_LOW over IN_PWR_HIGH when both match.
    """
    candidates = []
    seen_ports = set()
    for r in alarms:
        if r["alarm_name"] not in ("IN_PWR_LOW", "IN_PWR_HIGH"):
            continue
        if r.get("layer") != "OTS":
            continue
        port = r.get("port") or ""
        if not port:
            continue
        names_port = _names_at_port(alarms, port)
        if "IN_PWR_ABN" not in names_port:
            continue
        # NOT Exists OTS_LOS on the same span — rules out fiber_cut.
        span = r.get("container_id") or ""
        if span in topo.optical_links and "OTS_LOS" in _names_at_span(alarms, span):
            continue
        # NOT Exists BEFFEC_EXC on a span or OTU pipe at the same NE — that's
        # the fiber_attenuation_high signature.  BEFFEC_EXC sitting on a port
        # (the rx_power_drift L2 placement) does NOT block.
        ne = r.get("ne") or ""
        beffec_on_topo = any(
            a["alarm_name"] == "BEFFEC_EXC"
            and a.get("ne") == ne
            and (a.get("container_id") in topo.optical_links
                 or a.get("container_id") in topo.otn_links)
            for a in alarms
        )
        if beffec_on_topo:
            continue
        candidates.append(r)
        seen_ports.add(port)

    # One candidate per port: prefer IN_PWR_LOW, fall back to IN_PWR_HIGH.
    out = []
    for port in seen_ports:
        port_cands = [c for c in candidates if c.get("port") == port]
        low = next((c for c in port_cands if c["alarm_name"] == "IN_PWR_LOW"), None)
        pick = low or port_cands[0]
        out.append(Inferred("RootCandidate", pick["id"], pick["id"],
                            fault_type="rx_power_drift",
                            source="§8.4.rx_power_drift", weight=1.7))
    return out


def fp_ne_power_off(alarms, topo):
    """Design/08 §8.5 literal rule is ForAll spans-from-this-NE have OTS_LOS.
    With the simulator's drop_prob=5% non-root alarms can be missing, so we
    loosen to a majority quorum: 60%+ of the NE's outgoing spans must show
    OTS_LOS for the rule to fire. POWER_ABNORMAL on the NE is required.
    """
    out = []
    for r in alarms:
        if r["alarm_name"] != "POWER_ABNORMAL":
            continue
        ne = r.get("ne") or ""
        if not ne:
            continue
        out_spans = [l.link_id for l in topo.optical_links.values()
                     if l.from_ne == ne]
        if not out_spans:
            continue
        n_los = sum(1 for s in out_spans if "OTS_LOS" in _names_at_span(alarms, s))
        if n_los >= max(1, int(0.6 * len(out_spans))):
            out.append(Inferred("RootCandidate", r["id"], r["id"],
                                fault_type="ne_power_off",
                                source="§8.5.ne_power_off", weight=2.0))
    return out


def fp_board_power_off(alarms, topo):
    """Design/08 §8.6: BD_OFFLINE + peer-port R_LOS.

    Build the peer-port map from both optical-topo and otn-topo links so the
    rule fires for OTU/OA/FIU/SCC board outages (OTU boards have otn-topo
    ports, not optical-topo).  If no peer R_LOS is found anywhere, we still
    fire when BD_OFFLINE is present AND there's no OTS_LOS at the same NE —
    a softer signature for board outages that don't propagate to a peer
    (e.g. SCC / management boards).
    """
    out = []
    peer_of: dict[str, str] = {}
    for net in ("optical-topo", "otn-topo"):
        links = topo.optical_links if net == "optical-topo" else topo.otn_links
        for link in links.values():
            s = topo._find_tp(net, link.from_ne, link.from_tp)
            d = topo._find_tp(net, link.to_ne,   link.to_tp)
            if s and d and s.inventory_id and d.inventory_id:
                peer_of.setdefault(s.inventory_id, d.inventory_id)
                peer_of.setdefault(d.inventory_id, s.inventory_id)

    board_ports: dict[str, set[str]] = {}
    for ne in topo.nes.values():
        for b in ne.boards:
            board_ports[b.uuid] = {p.uuid for p in b.ports}

    for r in alarms:
        if r["alarm_name"] != "BD_OFFLINE":
            continue
        board = r.get("board") or ""
        if not board:
            continue
        ports = board_ports.get(board, set())
        peers = {peer_of[p] for p in ports if p in peer_of}
        peer_r_los = any(
            a["alarm_name"] == "R_LOS" and a.get("port") in peers
            for a in alarms
        )
        ne = r.get("ne") or ""
        no_ne_outage = "OTS_LOS" not in _names_at_ne(alarms, ne)
        if peer_r_los or no_ne_outage:
            out.append(Inferred("RootCandidate", r["id"], r["id"],
                                fault_type="board_power_off",
                                source="§8.6.board_power_off",
                                weight=2.0 if peer_r_los else 1.7))
    return out


def fp_fan_failure(alarms, topo):
    out = []
    for r in alarms:
        if r["alarm_name"] != "FAN_FAIL":
            continue
        ne = r.get("ne") or ""
        if not ne:
            continue
        # Exists TEMP_OVER on the same NE but a different board.
        same_ne_temp = [
            a for a in alarms
            if a["alarm_name"] == "TEMP_OVER"
            and a.get("ne") == ne
            and a.get("board") != r.get("board")
        ]
        if same_ne_temp:
            out.append(Inferred("RootCandidate", r["id"], r["id"],
                                fault_type="fan_failure",
                                source="§8.7.fan_failure", weight=2.0))
    return out


def fp_config_mismatch(alarms, topo):
    """The simulator marks OPU_PLM as the canonical root for config_mismatch;
    OTU_TIM and ODU_PM_TIM are the co-occurrence companions per Design/05.
    We only emit RootCandidate(OPU_PLM) — picking the right alarm matters.
    """
    out = []
    targets = {"OPU_PLM", "OTU_TIM", "ODU_PM_TIM"}
    for r in alarms:
        if r["alarm_name"] != "OPU_PLM":
            continue
        port = r.get("port") or ""
        if not port:
            continue
        # NOT Exists OTS_LOS on the same NE.
        if "OTS_LOS" in _names_at_ne(alarms, r.get("ne") or ""):
            continue
        # Three of {OPU_PLM, OTU_TIM, ODU_PM_TIM} co-occur on the same port.
        names_port = _names_at_port(alarms, port)
        if len(targets & names_port) >= 3:
            out.append(Inferred("RootCandidate", r["id"], r["id"],
                                fault_type="config_mismatch",
                                source="§8.8.config_mismatch", weight=1.8))
    return out


# Order matters for tie-breaking: more specific / higher-weight fingerprints first.
FINGERPRINTS = [
    fp_ne_power_off,
    fp_board_power_off,
    fp_fan_failure,
    fp_fiber_cut,
    fp_config_mismatch,
    fp_fiber_attenuation_high,
    fp_fec_pre_cor_high,
    fp_rx_power_drift,
    fp_b2_exc_high,         # B2 before B1 (B2 suppresses B1)
    fp_b1_exc_high,
]


def apply(alarms: list[dict], topo: Topology) -> list[Inferred]:
    out: list[Inferred] = []
    for fp in FINGERPRINTS:
        out.extend(fp(alarms, topo))
    return out
