"""Load the three YANG JSON instance files into a `Topology` object.

Reads:
  data/yang/topology.json   (/ietf-network:networks)
  data/yang/tunnels.json    (/ietf-te:te)
  data/yang/inventory.json  (/ietf-network-hardware-inventory:...)

Drops `_comment` keys (RFC 7951 ignores unknown keys) and resolves the
`ietf-hw-inventory-ref-topo:inventory-id` cross-reference between every
topology TP and its inventory port.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .topology import NE, Board, Link, Port, TP, Topology, Tunnel

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
YANG_DIR = os.path.join(ROOT, "data", "yang")


# ---------------------------------------------------------------------------

def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return _strip_comments(json.load(f))


def _strip_comments(obj: Any) -> Any:
    """Recursively drop keys starting with '_comment' (project annotations)."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items()
                if not k.startswith("_comment")}
    if isinstance(obj, list):
        return [_strip_comments(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# Inventory loader
# ---------------------------------------------------------------------------

def _load_inventory(path: str, t: Topology) -> None:
    raw = _load_json(path)["ietf-network-hardware-inventory:network-hardware-inventory"]
    for ne_entry in raw["network-elements"]["network-element"]:
        ne_id = ne_entry["uuid"]
        chassis = next(
            c for c in ne_entry["components"]["component"]
            if c.get("class") == "iana-hardware:chassis"
        )
        ne = NE(uuid=ne_id, chassis_uuid=chassis["uuid"])

        # group components by parent
        by_parent: dict[str, list[dict]] = {}
        for c in ne_entry["components"]["component"]:
            parent = c.get("parent", "")
            by_parent.setdefault(parent, []).append(c)

        for board_comp in by_parent.get(chassis["uuid"], []):
            cls = board_comp.get("class", "")
            board_uuid = board_comp["uuid"]
            board_name = board_comp.get("name", board_uuid.split(".")[-1])
            board_type = _board_type(board_name, cls)
            board = Board(uuid=board_uuid, name=board_name,
                          ne_id=ne_id, type=board_type)
            # PPE augmentations on OTU modules (Design/12 §6.1).  All keys
            # default to the Board dataclass defaults when absent.
            if "nfms:ppe-enabled" in board_comp:
                board.ppe_enabled = bool(board_comp["nfms:ppe-enabled"])
            if "nfms:ppe-delta-z-m" in board_comp:
                board.ppe_delta_z_m = int(board_comp["nfms:ppe-delta-z-m"])
            if "nfms:ppe-poll-interval-ms" in board_comp:
                board.ppe_poll_interval_ms = int(board_comp["nfms:ppe-poll-interval-ms"])
            if "nfms:dbp-noise-sigma-db" in board_comp:
                board.dbp_noise_sigma_db = float(board_comp["nfms:dbp-noise-sigma-db"])
            if "nfms:dbp-noise-sigma-per-km-db" in board_comp:
                board.dbp_noise_sigma_per_km_db = float(
                    board_comp["nfms:dbp-noise-sigma-per-km-db"])
            # PPE augmentations on OA modules.
            if "nfms:edfa-gain-db" in board_comp:
                board.edfa_gain_db = float(board_comp["nfms:edfa-gain-db"])
            if "nfms:edfa-noise-figure-db" in board_comp:
                board.edfa_noise_figure_db = float(board_comp["nfms:edfa-noise-figure-db"])
            if "nfms:edfa-z-km" in board_comp:
                board.edfa_z_km = float(board_comp["nfms:edfa-z-km"])
            for port_comp in by_parent.get(board_uuid, []):
                if port_comp.get("class") != "iana-hardware:port":
                    continue
                p = Port(uuid=port_comp["uuid"],
                         name=port_comp.get("name", port_comp["uuid"].split(".")[-1]),
                         ne_id=ne_id, board_id=board_uuid)
                board.ports.append(p)
                t.ports[p.uuid] = p
                # Per-OCH RX port marker (Design/00 §4.3): records which OCh
                # this receiver terminates.
                rx_och = port_comp.get("nfms:rx-och")
                if rx_och:
                    board.rx_ports[p.uuid] = rx_och
            ne.boards.append(board)
        t.nes[ne_id] = ne


def _board_type(name: str, hw_class: str) -> str:
    """Derive a coarse board type tag from the inventory naming convention."""
    if hw_class == "iana-hardware:fan":
        return "FAN"
    last = name.rsplit(".", 1)[-1].split("_", 1)[0]   # 'OA_W' -> 'OA'
    if last in ("OTU", "OA", "FIU", "SCC", "FAN"):
        return last
    return last or "UNKNOWN"


# ---------------------------------------------------------------------------
# Topology loader
# ---------------------------------------------------------------------------

def _load_topology(path: str, t: Topology) -> None:
    raw = _load_json(path)["ietf-network:networks"]["network"]
    for net in raw:
        net_id = net["network-id"]
        nodes_dest = t.optical_nodes if net_id == "optical-topo" else t.otn_nodes
        links_dest = t.optical_links if net_id == "optical-topo" else t.otn_links

        for n in net.get("node", []):
            ne_id = n["node-id"]
            tps: list[TP] = []
            for tp in n.get("ietf-network-topology:termination-point", []):
                tps.append(TP(
                    tp_id=tp["tp-id"],
                    ne_id=ne_id,
                    network=net_id,
                    inventory_id=tp.get("ietf-hw-inventory-ref-topo:inventory-id", ""),
                ))
            nodes_dest[ne_id] = tps

        for lk in net.get("ietf-network-topology:link", []):
            sup_link_id = ""
            for sl in lk.get("supporting-link", []):
                # we only model one supporting layer in v1
                sup_link_id = sl.get("link-ref", "")
                break
            distance = 0
            te = lk.get("ietf-te-topology:te", {}).get("te-link-attributes", {})
            otn_link = te.get("ietf-otn-topology:otn-link", {})
            distance = int(otn_link.get("distance", 0))
            # PPE augmentations on optical-topo span links (Design/12 §6.1).
            length_km = float(te.get("nfms:length-km", 0.0))
            fibre_type = te.get("nfms:fibre-type", "SMF-G.652")
            alpha = float(te.get("nfms:alpha-db-per-km", 0.20))
            connector_losses = []
            for cl in te.get("nfms:connector-losses", []):
                connector_losses.append({
                    "z_km":   float(cl["z-km"]),
                    "loss_db": float(cl["loss-db"]),
                })
            # Per-OCH PPE rework (decision D-4): inline EDFA structure.
            span_count = int(te.get("nfms:span-count", 0))
            amplifiers = []
            for amp in te.get("nfms:amplifiers", []):
                amplifiers.append({
                    "z_km":    float(amp["z-km"]),
                    "gain_db": float(amp["gain-db"]),
                })
            links_dest[lk["link-id"]] = Link(
                link_id=lk["link-id"],
                network=net_id,
                from_ne=lk["source"]["source-node"],
                from_tp=lk["source"]["source-tp"],
                to_ne=lk["destination"]["dest-node"],
                to_tp=lk["destination"]["dest-tp"],
                supporting_link_id=sup_link_id,
                distance_km=distance,
                length_km=length_km,
                fibre_type=fibre_type,
                alpha_db_per_km=alpha,
                connector_losses=connector_losses,
                span_count=span_count,
                amplifiers=amplifiers,
            )


# ---------------------------------------------------------------------------
# Tunnels loader
# ---------------------------------------------------------------------------

def _load_tunnels(path: str, t: Topology) -> None:
    raw = _load_json(path)["ietf-te:te"]["tunnels"]["tunnel"]
    for tn in raw:
        ttype = tn["type"].split(":")[-1]   # 'wdm-tunnel' or 'otn-tunnel'
        kind = "wdm" if ttype == "wdm-tunnel" else "otn"
        ero, ero_net = _read_ero(tn)
        sup_tnl = ""
        for s in tn.get("supporting-tunnel", []):
            sup_tnl = s.get("tunnel-ref", "")
            break
        rate = ""
        otn_bw = tn.get("te-bandwidth", {}).get("ietf-otn-tunnel:otn", {})
        rate = otn_bw.get("rate-type", "").split(":")[-1]
        client_svc = tn.get("network-fault-mgmt-state:client-service-name", "")

        # PPE augmentations: tx-channel-power (always there on WDM tunnels)
        # and the optional nfms:ppe-override block.
        tx_power = 0.0
        ppe_override: bool | None = None
        if kind == "wdm":
            primary = tn.get("primary-paths", {}).get("primary-path", [])
            if primary:
                wc = primary[0].get("ietf-wdm-tunnel:wdm-constraint", {})
                tx_power = float(wc.get("tx-channel-power", 0.0))
                ov = wc.get("nfms:ppe-override")
                if isinstance(ov, dict) and "enabled" in ov:
                    ppe_override = bool(ov["enabled"])

        tunnel = Tunnel(
            name=tn["name"],
            type=kind,
            src_ne=tn["source"],
            dst_ne=tn["destination"],
            src_tp_id=tn["src-tp-id"],
            dst_tp_id=tn["dst-tp-id"],
            ero=ero,
            ero_network=ero_net,
            supporting_tunnel=sup_tnl,
            client_service_name=client_svc,
            rate_type=rate,
            tx_channel_power_dbm=tx_power,
            ppe_override_enabled=ppe_override,
        )
        if kind == "wdm":
            t.wdm_tunnels[tunnel.name] = tunnel
        else:
            t.otn_tunnels[tunnel.name] = tunnel


def _read_ero(tn: dict) -> tuple[list[str], str]:
    primary = tn.get("primary-paths", {}).get("primary-path", [])
    if not primary:
        return [], ""
    eros = primary[0].get("explicit-route-objects", {}).get(
        "route-object-include-exclude", []
    )
    eros_sorted = sorted(eros, key=lambda h: h.get("index", 0))
    out: list[str] = []
    network = ""
    for hop in eros_sorted:
        nh = hop.get("type", {}).get("numbered-link-hop", {}).get("numbered-link-hop", {})
        sl = nh.get("supporting-link-ref", {})
        if sl.get("link-ref"):
            out.append(sl["link-ref"])
            network = sl.get("network-ref", network)
    return out, network


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def load(yang_dir: str | None = None) -> Topology:
    yang_dir = yang_dir or YANG_DIR
    t = Topology()
    _load_inventory(os.path.join(yang_dir, "inventory.json"), t)
    _load_topology(os.path.join(yang_dir, "topology.json"), t)
    _load_tunnels(os.path.join(yang_dir, "tunnels.json"), t)
    return t


if __name__ == "__main__":
    t = load()
    print(f"Loaded YANG topology, id={t.topology_id()}")
    print(f"  NEs:           {list(t.nes)}")
    print(f"  optical-links: {list(t.optical_links)}")
    print(f"  otn-links:     {list(t.otn_links)}")
    print(f"  WDM tunnels:   {list(t.wdm_tunnels)}")
    print(f"  OTN tunnels:   {list(t.otn_tunnels)}")
    print(f"  Inventory ports: {len(t.ports)}")
    # Spot-check the otn -> optical underlay relationship
    for l in t.otn_links.values():
        print(f"   {l.link_id} supports span={l.supporting_link_id}")
    # Spot-check tunnel ERO + service tags
    for tn in t.otn_tunnels.values():
        print(f"   {tn.name} ero={tn.ero}  svc={tn.client_service_name}  "
              f"rides on {tn.supporting_tunnel}")
