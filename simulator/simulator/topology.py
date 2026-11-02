"""In-memory topology model populated from the three YANG instance files.

Schema mirrors what `data/yang/{topology,tunnels,inventory}.json` carries —
optical-topo + otn-topo networks, OCh/ODU tunnels, NE/board/port inventory.
The simulator's fault rules and the engine consume this object.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Node entities (ports, boards, NEs)
# ---------------------------------------------------------------------------

@dataclass
class Port:
    """An inventory port component, with its cross-reference into topology."""
    uuid: str            # inventory uuid (= dotted name in v1, e.g. ne1.FIU.line_to_ne2)
    name: str            # local name (last segment)
    ne_id: str
    board_id: str        # parent board uuid


@dataclass
class Board:
    """An inventory module/fan component."""
    uuid: str
    name: str
    ne_id: str
    type: str            # 'OTU' / 'OA' / 'FIU' / 'SCC' / 'FAN'  (derived from name)
    ports: list[Port] = field(default_factory=list)
    # Per-OCH PPE rework (Design/00 §4.3): OTU-only.  Maps an RX port uuid to
    # the OCh it terminates (drops).  One entry per OCh dropping at this NE,
    # so ``len(rx_ports)`` == number of dropping OChs (C=4, F=2).
    rx_ports: dict[str, str] = field(default_factory=dict)

    # ----- PPE augmentations (Design/12 §6.1) ------------------------------
    # OTU-only.  Defaults match the code-side defaults in `ppe.py`, so a
    # board without any nfms:* field still produces a coherent baseline.
    ppe_enabled: bool = False
    ppe_delta_z_m: int = 200
    ppe_poll_interval_ms: int = 30_000
    dbp_noise_sigma_db: float = 0.05
    # Per-OCH rework: the z-axis is now the *whole* route (up to ~160 km), so
    # the per-km term must stay small or the cumulative DBP noise swamps fine
    # features.  0.001 dB/km → σ ≈ 0.05 + 0.16 = 0.21 dB at 160 km, realistic
    # for a coherent DBP power estimate.
    dbp_noise_sigma_per_km_db: float = 0.001
    # OA-only.  `edfa_gain_db` is informational here: the PPE composer takes all
    # amplifier *gain* from the topology `nfms:amplifiers` list (decision D-4),
    # not from inventory, so this field does not feed the sawtooth.
    edfa_gain_db: float | None = None
    # `edfa_noise_figure_db` DOES feed the model: each node OA the OCh route
    # transits adds an ASE σ(z) step scaled by its linear noise factor
    # (ppe._route_ase_amps / ppe._noise).  `edfa_z_km` positions that OA within
    # the span it launches (0 = at NE egress).
    edfa_noise_figure_db: float = 5.0
    edfa_z_km: float = 0.0


@dataclass
class NE:
    uuid: str
    chassis_uuid: str    # the chassis component
    boards: list[Board] = field(default_factory=list)

    def board(self, board_id: str) -> Board | None:
        for b in self.boards:
            if b.uuid == board_id or b.name == board_id:
                return b
        return None


# ---------------------------------------------------------------------------
# Topology entities (network links and TPs)
# ---------------------------------------------------------------------------

@dataclass
class TP:
    """A topology termination-point (lighter-weight than an inventory port)."""
    tp_id: str           # tp-id inside the node, e.g. 'fiu-tx-to-ne2'
    ne_id: str
    network: str         # 'optical-topo' or 'otn-topo'
    inventory_id: str = ""   # leafref to a Port.uuid


@dataclass
class Link:
    link_id: str
    network: str          # 'optical-topo' or 'otn-topo'
    from_ne: str
    from_tp: str
    to_ne: str
    to_tp: str
    supporting_link_id: str = ""   # otn-topo link supports an optical-topo span
    distance_km: int = 0           # for OTN links

    # ----- PPE augmentations (Design/12 §6.1) ------------------------------
    # Optical-topo only.  `length_km = 0.0` is the "unset" sentinel — a
    # zero-length span gets no PPE trace.  The Design/03 §2.3 minimum viable
    # set requires every optical span to carry a non-zero length-km.
    length_km: float = 0.0
    fibre_type: str = "SMF-G.652"
    alpha_db_per_km: float = 0.20
    # Each entry: {"z_km": float, "loss_db": float}.
    connector_losses: list[dict] = field(default_factory=list)
    # ----- per-OCH PPE rework (Design/00 §4.3, decision D-4) ----------------
    # An OMS is built of `span_count` equal physical fibre spans, each closed
    # by an inline EDFA (the last one being the node OA at the OMS boundary).
    # `amplifiers` is the explicit per-span EDFA list — each entry
    # {"z_km": float (offset within THIS span), "gain_db": float}.  When it is
    # empty the PPE composer auto-derives `span_count` equal-spaced EDFAs from
    # `nfms:length-km` / `nfms:span-count` (see ppe.amplifiers_for).
    span_count: int = 0
    amplifiers: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------

@dataclass
class Tunnel:
    name: str
    type: str                       # 'wdm' or 'otn'
    src_ne: str
    dst_ne: str
    src_tp_id: str
    dst_tp_id: str
    ero: list[str] = field(default_factory=list)         # ordered list of link-refs
    ero_network: str = ""                                # which network the ero hops belong to
    supporting_tunnel: str = ""                          # otn -> wdm (the OCh underlay)
    client_service_name: str = ""                        # nfms tag, otn tunnels only
    rate_type: str = ""                                  # e.g. 'odu-2'

    # ----- PPE augmentations (Design/12 §6.1) ------------------------------
    # Source-side launch power, from `ietf-wdm-tunnel:wdm-constraint/
    # tx-channel-power` on each OCh tunnel.  WDM tunnels only.
    tx_channel_power_dbm: float = 0.0
    # Per-channel PPE override; absent in YANG -> None means "use the OTU
    # board's ppe_enabled".  When False, this channel emits no PPE.
    ppe_override_enabled: bool | None = None


# ---------------------------------------------------------------------------
# Top-level container
# ---------------------------------------------------------------------------

@dataclass
class Topology:
    name: str = "diamond+crossbar"

    # inventory
    nes: dict[str, NE] = field(default_factory=dict)
    ports: dict[str, Port] = field(default_factory=dict)         # by uuid

    # topology layers
    optical_nodes: dict[str, list[TP]] = field(default_factory=dict)   # ne_id -> tps
    otn_nodes: dict[str, list[TP]] = field(default_factory=dict)
    optical_links: dict[str, Link] = field(default_factory=dict)       # by link_id
    otn_links: dict[str, Link] = field(default_factory=dict)

    # tunnels
    wdm_tunnels: dict[str, Tunnel] = field(default_factory=dict)
    otn_tunnels: dict[str, Tunnel] = field(default_factory=dict)

    # ----- helpers used by the propagation engine ---------------------------

    def otn_links_supported_by(self, optical_link_id: str) -> list[Link]:
        """Return all OTN links whose supporting-link is the given optical span."""
        return [l for l in self.otn_links.values()
                if l.supporting_link_id == optical_link_id]

    def wdm_tunnels_using(self, optical_link_id: str) -> list[Tunnel]:
        return [t for t in self.wdm_tunnels.values() if optical_link_id in t.ero]

    def otn_tunnels_using(self, otn_link_id: str) -> list[Tunnel]:
        return [t for t in self.otn_tunnels.values() if otn_link_id in t.ero]

    def link_endpoints(self, link: Link) -> tuple[str, str]:
        """Return (rx_ne_id, rx_inventory_port_id) for a link's destination side."""
        rx_tp = self._find_tp(link.network, link.to_ne, link.to_tp)
        return link.to_ne, rx_tp.inventory_id if rx_tp else ""

    def _find_tp(self, network: str, ne_id: str, tp_id: str) -> TP | None:
        nodes = self.optical_nodes if network == "optical-topo" else self.otn_nodes
        for tp in nodes.get(ne_id, []):
            if tp.tp_id == tp_id:
                return tp
        return None

    def board_of_port(self, inventory_port_id: str) -> str:
        port = self.ports.get(inventory_port_id)
        return port.board_id if port else ""

    def topology_id(self) -> str:
        """Stable hash of the topology shape — for incidents.db reproducibility column."""
        import hashlib
        sig = "|".join(sorted(self.optical_links) + sorted(self.otn_links)
                       + sorted(self.wdm_tunnels) + sorted(self.otn_tunnels))
        return hashlib.sha1(sig.encode()).hexdigest()[:12]
