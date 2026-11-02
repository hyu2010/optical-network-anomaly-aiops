"""Triple dataclass, entity URI helpers, predicate names, container classifier.

Mirrors §3 of `Design/09_knowledge_graph_builder.md`. Every node in the KG
gets a `{type_prefix}:{identifier}` URI to keep namespaces disjoint.
"""
from __future__ import annotations

from dataclasses import dataclass

from simulator.simulator.topology import Topology


# ---------------------------------------------------------------------------
# Triple
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Triple:
    subject: str
    predicate: str
    object: str
    source: str = "topology"      # topology / alarm / derived / rule:Rn
    weight: float = 1.0
    incident_id: int = 0           # 0 for topology triples


# ---------------------------------------------------------------------------
# Entity URI helpers
# ---------------------------------------------------------------------------

def alarm_uri(incident_id: int, alarm_id: int) -> str:
    return f"alarm:{incident_id}:{alarm_id}"


def ne_uri(ne_id: str) -> str:        return f"ne:{ne_id}"
def board_uri(uuid: str) -> str:      return f"board:{uuid}"
def port_uri(uuid: str) -> str:       return f"port:{uuid}"
def span_uri(link_id: str) -> str:    return f"span:{link_id}"
def otu_uri(link_id: str) -> str:     return f"otu:{link_id}"
def och_uri(name: str) -> str:        return f"och:{name}"
def odu_uri(name: str) -> str:        return f"odu:{name}"
def svc_uri(name: str) -> str:        return f"svc:{name}"
def layer_uri(name: str) -> str:      return f"layer:{name}"


# PPE entities (Design/00 §4.5) — keyed per OCh-drop, not per span.
def ppe_trace_uri(incident_id: int, och: str) -> str:
    return f"ppe:{incident_id}:{och}"


def ppe_anom_uri(incident_id: int, och: str, idx: int) -> str:
    return f"ppe_anom:{incident_id}:{och}:{idx}"


def fpos_uri(span_id: str, z_km: float) -> str:
    """Quantised fibre-position URI — decimetre granularity (z_km * 10, truncated)."""
    return f"fpos:{span_id}:{int(round(z_km * 10))}"


# ---------------------------------------------------------------------------
# Predicate names (Design/09 §3.2)
# ---------------------------------------------------------------------------

# Topology
HOSTS              = "Hosts"
CONNECTED          = "Connected"
CARRIED_BY         = "CarriedBy"
RIDES_OVER         = "RidesOver"
SUPPORTING_TUNNEL  = "SupportingTunnel"
CARRIES            = "Carries"

# Alarm-location
IS_LAYER           = "IsLayer"
AT_NE              = "AtNE"
AT_BOARD           = "AtBoard"
AT_PORT            = "AtPort"
AT_SPAN            = "AtSpan"
ON_PIPE            = "OnPipe"
AT_CHANNEL         = "AtChannel"
CONTAINER          = "Container"

# Derived
EARLIER            = "Earlier"
CLOSE              = "Close"
WITHIN_SIX         = "WithinSix"
ON_SAME_NE         = "OnSameNE"
ON_SAME_BOARD      = "OnSameBoard"
ON_SAME_PORT       = "OnSamePort"
ON_SAME_CONTAINER  = "OnSameContainer"
SAME_SPAN          = "SameSpan"
ON_SAME_LINK       = "OnSameLink"
ALL_CLOSE          = "AllClose"        # compact flag — Design/09 §4 Stage 3
ALL_WITHIN_SIX     = "AllWithinSix"

# Targets / labels
ROOT_CAUSE         = "RootCause"
SECONDARY          = "Secondary"
SAME_INCIDENT      = "SameIncident"
CORRELATED         = "Correlated"

# PPE predicates (Design/12 §8.2)
MEASURED_ON        = "MeasuredOn"        # PpeTrace -> OChTunnel
MEASURES_SPAN      = "MeasuresSpan"      # PpeTrace -> Span
MEASURED_BY        = "MeasuredBy"        # PpeTrace -> Board (drop OTU)
HAS_ANOMALY        = "HasAnomaly"        # PpeTrace -> PpeAnomaly
LOCATED_AT         = "LocatedAt"         # PpeAnomaly -> FibrePosition
ON_SPAN            = "OnSpan"            # FibrePosition -> Span
PPE_INDICATES      = "PpeIndicates"      # PpeAnomaly -> Alarm (cross-modal)
PPE_ROOT_CAUSE     = "PpeRootCause"      # PpeAnomaly -> "true"


# ---------------------------------------------------------------------------
# Container classifier (Design/09 §3.3)
# ---------------------------------------------------------------------------

def classify_container(container_id: str, topo: Topology) -> str:
    """Return one of: span, otu, och, odu, port, other."""
    if not container_id:
        return "other"
    if container_id in topo.optical_links: return "span"
    if container_id in topo.otn_links:     return "otu"
    if container_id in topo.wdm_tunnels:   return "och"
    if container_id in topo.otn_tunnels:   return "odu"
    if container_id in topo.ports:         return "port"
    return "other"


def container_uri(container_id: str, kind: str) -> str:
    if kind == "span": return span_uri(container_id)
    if kind == "otu":  return otu_uri(container_id)
    if kind == "och":  return och_uri(container_id)
    if kind == "odu":  return odu_uri(container_id)
    if kind == "port": return port_uri(container_id)
    return f"other:{container_id}"
