"""Vendor alarm catalogue -> optx:AlarmType (doc 03 §4, §D of the KB).

Reads the published catalogue files in `data/`:
  alarm_catalog_9800.json, alarm_catalog.json   names, severities, descriptions
  alarms_detail.json, alarms_detail_9800.json   "Possible Causes" text
  alarm_suppression.json                         upstream -> downstream edges
Every alarm type gets a layer (vendor naming family), a Condition, its
suppression edges and cause-keyword tags.  Curated entries in the KB win.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from ..namespaces import DX, KB, OPTX

_RATE_RE = re.compile(r"^([A-Z]+)([0-9]+G?|k)(_.+)?$")
_RATE_FAMILIES = {"OTU", "ODU", "OPU"}

# Cause keywords of the vendor documentation (narrow, physical).
KEYWORD_PATTERNS: dict[str, re.Pattern] = {
    "fiber":         re.compile(r"\b(fiber|cable|connector|fiber cut|line performance|cut)\b", re.I),
    "los":           re.compile(r"\b(LOS|loss of signal|loss of light|signal lost|signal failure|loss of frame)\b", re.I),
    "laser":         re.compile(r"\b(laser|optical module|XFP|SFP|transmit (?:end|side))\b", re.I),
    "optical_power": re.compile(r"\b(optical power|input power|output power|attenuation|insertion loss)\b", re.I),
    "power_supply":  re.compile(r"\b(power supply|power source|power input|-48v|pem|voltage)\b", re.I),
    "temp":          re.compile(r"\b(temperature|cooling|fan|over.?temp|heat)\b", re.I),
    "biterr":        re.compile(r"\b(bit error|B1\b|B2\b|FEC|degrade|excessive errors|errors over)\b", re.I),
    "config":        re.compile(r"\b(configuration|misconfigured|mismatch|inconsistent|wrong|invalid setting|trail|TIM|PLM)\b", re.I),
}

LAYER_IRI = {"OTS": OPTX.OTS, "OMS": OPTX.OMS, "OCH": OPTX.OCH, "OTU": OPTX.OTU, "ODU": OPTX.ODU,
             "SDH": OPTX.SDH, "FEC": OPTX.FEC, "client": OPTX.CLIENT, "equipment": OPTX.EQUIPMENT,
             "environment": OPTX.ENVIRONMENT, "protection": OPTX.PROTECTION, "auto-power": OPTX.AUTOPOWER,
             "clock": OPTX.CLOCK, "management": OPTX.MANAGEMENT, "other": OPTX.OTHER}
TRANSPORT = {"OTS", "OMS", "OCH", "OTU", "ODU", "SDH", "FEC"}


def normalise(name: str) -> str:
    """OTUk_SSF / OTU2_SSF -> OTU_SSF (rate suffix of the OTN families)."""
    m = _RATE_RE.match(name)
    if not m or m.group(1) not in _RATE_FAMILIES:
        return name
    return m.group(1) + (m.group(3) or "")


def layer_family(name: str) -> str:
    """Vendor naming family of an alarm type (catalogue convention)."""
    if name.startswith("OTS"):
        return "OTS"
    if name.startswith("OMS"):
        return "OMS"
    if name.startswith(("OCh", "OCH")):
        return "OCH"
    if name.startswith("OTU"):
        return "OTU"
    if name.startswith(("ODU", "OPU")):
        return "ODU"
    if name.startswith(("MS_", "AU_", "HP_", "LP_", "B1_", "B2_", "B3_")):
        return "SDH"
    if name.startswith(("R_LOS", "R_LOF", "MUT_LOS", "CHAN_LOS")):
        return "OTS"
    if name.startswith(("ALS_", "LSR_", "LASER_", "ALM_ALS")):
        return "equipment"
    if name.startswith(("IN_PWR", "OUT_PWR")):
        return "OTS"
    if name.startswith(("FEC", "BEFFEC")):
        return "OTU"
    if name.startswith(("FAN_", "TEMP_", "AMBIENT_", "PUMP_COOL")):
        return "environment"
    if name.startswith(("POWER_", "PWR_", "VOLT")):
        return "equipment"
    if name.startswith(("BD_", "HARD_", "BUS_", "FW_BD", "WRG_BD")):
        return "equipment"
    if name.startswith(("ETH_", "GFP_", "LINK_", "FC_", "GE_")):
        return "client"
    if name.startswith(("APS_", "TPS_", "DLAG", "OLP_", "SNCP", "PROTECT", "PROTOCOL")):
        return "protection"
    if name.startswith(("ALC_", "APE_", "IPA_", "OPA_")):
        return "auto-power"
    if name.startswith(("CLK", "CLOCK", "SYNC")):
        return "clock"
    if name.startswith(("COMMUN_", "ECC_", "NE_OFFLINE", "DCN")):
        return "management"
    return "other"


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[_\-]", name.upper()) if t]


def condition_for(name: str, layer: str) -> URIRef:
    """Interpretation of an alarm type as a Condition (never as a Cause)."""
    n = name.upper()
    toks = _tokens(name)
    if n in ("IN_PWR_LOW", "SUM_INPWR_LOW", "INPWR_LOW"):
        return OPTX.LowInputPower
    if n in ("IN_PWR_HIGH", "SUM_INPWR_HIGH"):
        return OPTX.HighInputPower
    if n in ("IN_PWR_ABN", "RX_POWER_ABNORMAL", "IN_PWR_FAIL"):
        return OPTX.InputPowerAbnormal
    if n.startswith(("FAN_",)):
        return OPTX.CoolingFailure
    if n.startswith(("TEMP_", "AMBIENT_TEMP")) or "TEMP_OVER" in n:
        return OPTX.OverTemperature
    if n in ("BD_OFFLINE", "HARD_BAD", "POWER_ABNORMAL", "POWER_FAIL", "BD_NOT_INSTALLED", "NE_POWER_FAIL") \
            or (layer == "equipment" and ("OFFLINE" in toks or "FAIL" in toks or "BAD" in toks)):
        return OPTX.EquipmentDown
    if layer in TRANSPORT:
        if "LOS" in toks:
            return OPTX.LossOfSignal
        if any(t in toks for t in ("TIM", "PLM", "SLM", "MSIM")):
            return OPTX.TraceMismatch
        if any(t in toks for t in ("BDI", "RDI", "BEI")):
            return OPTX.BackwardDefect
        if any(t in toks for t in ("SSF", "AIS", "FDI", "LOF", "LOM", "LOP", "UNEQ", "LOFLOM", "OCI", "LCK", "SF")):
            return OPTX.ServerSignalFail
        if n.startswith("B1_"):
            return OPTX.RegeneratorSectionErrors
        if n.startswith("B2_"):
            return OPTX.MultiplexSectionErrors
        if n.startswith(("BEFFEC", "FEC_")) or "FEC" in toks:
            return OPTX.PreFecDegraded
        if any(t in toks for t in ("DEG", "EXC", "SD", "ERR")):
            return OPTX.SignalDegraded
    return OPTX.OtherCondition


def alarm_type_iri(name: str) -> URIRef:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return KB[f"AT_{safe}"]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def catalog_graph(data_dir: Path, curated: Graph | None = None) -> Graph:
    """Build the alarm-type graph from the vendor catalogue files."""
    g = Graph()
    names: dict[str, dict] = {}

    for fname in ("alarm_catalog_9800.json", "alarm_catalog.json", "alarms_index.json"):
        data = _load(data_dir / fname) or {}
        for key, entry in data.items():
            name = normalise(str(entry.get("name") or key).split("::", 1)[0].strip())
            if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name) or name == "Table":
                continue
            info = names.setdefault(name, {"severity": None, "causes": []})
            if not info["severity"] and entry.get("severity"):
                info["severity"] = str(entry["severity"])
            info["causes"] += [str(c) for c in entry.get("possible_causes", []) or []]

    for fname in ("alarms_detail.json", "alarms_detail_9800.json"):
        data = _load(data_dir / fname) or {}
        for key, entry in data.items():
            name = normalise(key.split("::", 1)[0])
            info = names.setdefault(name, {"severity": entry.get("severity"), "causes": []})
            info["causes"] += [str(c) for c in entry.get("possible_causes", []) or []]

    edges = _load(data_dir / "alarm_suppression.json") or []
    for u, d in edges:
        for n in (normalise(u), normalise(d)):
            names.setdefault(n, {"severity": None, "causes": []})

    curated_cond = set()
    if curated is not None:
        for at in curated.subjects(RDF.type, OPTX.AlarmType):
            if (at, OPTX.indicatesCondition, None) in curated:
                curated_cond.add(at)

    for name, info in names.items():
        at = alarm_type_iri(name)
        layer = layer_family(name)
        g.add((at, RDF.type, OPTX.AlarmType))
        g.add((at, OPTX.alarmName, Literal(name)))
        if curated is None or (at, OPTX.alarmLayer, None) not in curated:
            g.add((at, OPTX.alarmLayer, LAYER_IRI[layer]))
        if at not in curated_cond:
            g.add((at, OPTX.indicatesCondition, condition_for(name, layer)))
        if info["severity"] and (curated is None or (at, OPTX.defaultSeverity, None) not in curated):
            g.add((at, OPTX.defaultSeverity, Literal(info["severity"])))
        text = " ".join(info["causes"])
        if text:
            g.add((at, OPTX.possibleCauseText, Literal(text[:2000])))
            for kw, pat in KEYWORD_PATTERNS.items():
                if pat.search(text):
                    g.add((at, OPTX.causeKeyword, Literal(kw)))

    for u, d in edges:
        g.add((alarm_type_iri(normalise(u)), OPTX.suppresses, alarm_type_iri(normalise(d))))
    return g


def alarm_types_yang(names: list[str], layer_of: dict[str, str]) -> str:
    """Generate the full diag-agent-alarm-types YANG module (one identity per alarm name)."""
    bases = {"OTS": "ots-layer", "OMS": "ots-layer", "OCH": "och-layer", "OTU": "otu-layer", "ODU": "odu-layer",
             "SDH": "sdh-layer", "FEC": "otu-layer", "equipment": "equipment", "environment": "environment"}
    out = ["module diag-agent-alarm-types {", "  yang-version 1.1;",
           '  namespace "urn:example:yang:diag-agent-alarm-types";', "  prefix daat;", "",
           "  import ietf-alarms {", "    prefix al;", "  }", "",
           '  organization\n    "optical-network-anomaly-aiops";',
           '  contact\n    "diag_agent maintainers";',
           '  description\n    "Alarm type identities (RFC 8632 alarm-type-id) for the test bed\'s\n'
           "     alarm catalogue.  Generated by diag_agent.semantic_memory.loaders.catalog_to_rdf\n"
           '     from the vendor catalogue (one identity per alarm name).";', "",
           "  revision 2026-11-02 {", '    description\n      "Generated from the vendor catalogue.";', "  }", "",
           "  identity optical-alarm {", "    base al:alarm-type-id;",
           '    description\n      "Base of all test-bed alarm types.";', "  }"]
    for b in ("ots-layer", "och-layer", "otu-layer", "odu-layer", "sdh-layer", "equipment", "environment", "other-layer"):
        out += [f"  identity {b} {{", "    base optical-alarm;", "  }"]
    for n in sorted(set(names)):
        base = bases.get(layer_of.get(n, "other"), "other-layer")
        out += [f"  identity {n} {{", f"    base {base};", "  }"]
    out.append("}")
    return "\n".join(out) + "\n"
