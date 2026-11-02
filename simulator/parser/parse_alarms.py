"""Parse the Huawei OSN Alarms-Reference text into structured JSON.

Produces three artifacts in data/:
  - alarms_index.json         : {alarm_name: {severity, service_affecting, description}}
  - alarms_detail.json        : {alarm_name: {description, severity, type,
                                              parameters, impact, possible_causes:[...],
                                              procedure_steps:[...]}}
  - alarm_suppression.json    : [[upstream, downstream], ...]  (curated from sec. 1.7)

The parser is intentionally permissive — the source text is OCR-style with
inconsistent spacing, so we anchor on stable section markers and treat parse
failures as best-effort.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_TXT = os.path.join(ROOT, "Library", "extracted", "alarms.txt")
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)


# ---------------------------------------------------------------------------
# Section 2.1 — Product Alarm List (alarm name | description | severity | SA)
# ---------------------------------------------------------------------------

SEVERITIES = {"Critical", "Major", "Minor", "Warning"}
SERVICE_AFFECT = {"Yes", "No"}

# An alarm name starts with an uppercase letter and may contain digits,
# underscores, and lowercase letters in compound tokens (e.g. ALM_GFP_dCSF).
NAME_RE = re.compile(r"^([A-Z][A-Za-z0-9_]+)\s*(.*)$")
TAIL_RE = re.compile(
    r"^(.*?)\s*(Critical|Major|Minor|Warning)\s+(Yes|No)\s*$"
)
JUNK_RE = re.compile(
    r"^(?:"
    r"OptiX OSN.*|"
    r"Issue 01.*|"
    r"Copyright.*|"
    r"Alarms and Performance Events Reference.*|"
    r"Huawei Proprietary.*|"
    r"\d+|"
    r"Name Description.*|"
    r"Affecting|"
    r"2\.1.*|"
    r"2\.2.*|"
    r"This topic.*|"
    r"This section.*"
    r")\s*$"
)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def split_pages(text: str) -> list[tuple[int, str]]:
    """Return [(page_number, page_text), ...]."""
    parts = re.split(r"\n=====\s*PAGE\s+(\d+)\s*=====\n", text)
    out: list[tuple[int, str]] = []
    for i in range(1, len(parts), 2):
        out.append((int(parts[i]), parts[i + 1]))
    return out


def parse_index(pages: list[tuple[int, str]]) -> dict[str, dict]:
    """Walk Section 2.1 (Alarm List A..X) and build name -> entry map.

    Each table row looks like one of:
        SHORT_NAME short description Major No
        LONG_NAME first line of description
        more description ...
        Critical Yes

    We accumulate lines per row until we hit a "<Severity> <Yes|No>"
    terminator, then commit.
    """
    in_section = False
    relevant: list[str] = []
    for pno, body in pages:
        if pno < 75:  # before printed body (TOC range)
            continue
        if "2.1.1 Alarm List A" in body:
            in_section = True
        if in_section and "2.2 Board Alarm List" in body:
            relevant.append(body)
            break
        if in_section:
            relevant.append(body)

    raw = "\n".join(relevant)
    lines: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or JUNK_RE.match(s):
            continue
        lines.append(s)

    index: dict[str, dict] = {}
    cur_name: str | None = None
    cur_desc: list[str] = []
    for ln in lines:
        tail = TAIL_RE.match(ln)
        if tail:
            head = tail.group(1).strip()
            sev = tail.group(2)
            sa = tail.group(3) == "Yes"
            if cur_name is None:
                # single-line row: name + description on the same line as severity
                m = NAME_RE.match(head)
                if m:
                    name = m.group(1)
                    desc = m.group(2).strip()
                    index[name] = {
                        "name": name,
                        "description": desc,
                        "severity": sev,
                        "service_affecting": sa,
                    }
            else:
                desc = " ".join(cur_desc + ([head] if head else [])).strip()
                index[cur_name] = {
                    "name": cur_name,
                    "description": desc,
                    "severity": sev,
                    "service_affecting": sa,
                }
            cur_name = None
            cur_desc = []
            continue
        # not a terminator: start or continue a row
        m = NAME_RE.match(ln)
        if m and cur_name is None:
            cur_name = m.group(1)
            rest = m.group(2).strip()
            cur_desc = [rest] if rest else []
        elif cur_name is not None:
            cur_desc.append(ln)
        # else: stray line, ignore

    return index


# ---------------------------------------------------------------------------
# Section 3+ — Detailed alarm entries
# ---------------------------------------------------------------------------

@dataclass
class AlarmDetail:
    name: str
    variant: str = ""           # "OCS" / "OTN" / ""
    description: str = ""
    severity: str = ""
    alarm_type: str = ""
    parameters: str = ""
    impact: str = ""
    fault_symptom: str = ""
    possible_causes: list[str] = field(default_factory=list)
    procedure: str = ""
    related: str = ""


# heading like "3.1 B1_EXC", "3.1.1 B1_EXC (OCS)"
HEAD_RE = re.compile(r"^(\d+(?:\.\d+){1,3})\s+([A-Z][A-Z0-9_]+)(?:\s*\(([A-Za-z]+)\))?\s*$", re.MULTILINE)
FIELD_NAMES = [
    "Description",
    "Attribute",
    "Parameters",
    "Impact on the System",
    "Fault Symptom",
    "Possible Causes",
    "Procedure",
    "Related Information",
]


def join_pages(pages: list[tuple[int, str]], start_page: int) -> str:
    """Concatenate page bodies from start_page onwards into one string."""
    out = []
    for p, body in pages:
        if p < start_page:
            continue
        out.append(body)
    return "\n".join(out)


def parse_details(pages: list[tuple[int, str]]) -> dict[str, AlarmDetail]:
    """Walk Section 3+ and extract per-alarm structured data.

    Strategy: locate the start of section 3 (the first heading that
    matches HEAD_RE with leading "3."), then scan for headings sequentially
    and slice text between them.
    """
    # Find the page where section 3 begins (heading "3 Clearing Common Alarms"
    # or first numeric-3 heading). We just scan all pages and collect headings.
    headings: list[tuple[int, int, str, str, str]] = []
    full_text_parts: list[str] = []
    offsets: list[int] = []  # cumulative char offset per page
    cursor = 0
    for p, body in pages:
        offsets.append(cursor)
        full_text_parts.append(body)
        cursor += len(body) + 1  # +1 for the join newline below
    full_text = "\n".join(full_text_parts)

    # collect heading positions in detail-bearing sections (3, 4, 6)
    detail_section_prefixes = ("3.", "4.", "6.")
    for m in HEAD_RE.finditer(full_text):
        sec = m.group(1)
        if not sec.startswith(detail_section_prefixes):
            continue
        name = m.group(2)
        variant = m.group(3) or ""
        # Skip top-level "X.Y NAME" if a deeper sub-section carries the
        # actual content (the "Possible Causes" gate below handles this).
        headings.append((m.start(), m.end(), sec, name, variant))

    details: dict[str, AlarmDetail] = {}
    for i, (start, end, sec, name, variant) in enumerate(headings):
        # body is from end of this heading to start of next
        body_start = end
        body_end = headings[i + 1][0] if i + 1 < len(headings) else len(full_text)
        body = full_text[body_start:body_end]
        # Heuristic: if the body contains no "Possible Causes" within ~3KB,
        # this is probably an index-style heading (3.1 B1_EXC pointing at sub-pages).
        if "Possible Causes" not in body[:6000]:
            continue
        d = _parse_one_detail_block(name, variant, body)
        # If we already have an entry with same name+variant and the new one
        # has more content, keep the larger one.
        key = f"{name}::{variant}" if variant else name
        if key in details and len(details[key].description) >= len(d.description):
            continue
        details[key] = d
    return details


def _parse_one_detail_block(name: str, variant: str, body: str) -> AlarmDetail:
    d = AlarmDetail(name=name, variant=variant)
    # Find each labeled section by scanning for our known field markers.
    positions: list[tuple[int, str]] = []
    for fld in FIELD_NAMES:
        # Fields appear on their own line. Use a permissive multiline anchor.
        for m in re.finditer(rf"(?m)^{re.escape(fld)}\s*$", body):
            positions.append((m.start(), fld))
    positions.sort()

    for idx, (pos, fld) in enumerate(positions):
        sec_start = pos + len(fld)
        sec_end = positions[idx + 1][0] if idx + 1 < len(positions) else len(body)
        chunk = body[sec_start:sec_end].strip()
        if fld == "Description":
            d.description = _clean(chunk)
        elif fld == "Attribute":
            sev, typ = _parse_attribute(chunk)
            d.severity = sev
            d.alarm_type = typ
        elif fld == "Parameters":
            d.parameters = _clean(chunk)
        elif fld == "Impact on the System":
            d.impact = _clean(chunk)
        elif fld == "Fault Symptom":
            d.fault_symptom = _clean(chunk)
        elif fld == "Possible Causes":
            d.possible_causes = _split_causes(chunk)
        elif fld == "Procedure":
            d.procedure = _clean(chunk)
        elif fld == "Related Information":
            d.related = _clean(chunk)
    return d


def _clean(s: str) -> str:
    # collapse runs of whitespace and strip pagination junk
    s = re.sub(r"OptiX OSN [^\n]*\n", "", s)
    s = re.sub(r"Issue 01[^\n]*\n", "", s)
    s = re.sub(r"Copyright[^\n]*\n", "", s)
    s = re.sub(r"Huawei Proprietary[^\n]*\n", "", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _parse_attribute(chunk: str) -> tuple[str, str]:
    # Format: "Alarm Severity Alarm Type\n<sev> <type>"
    sev = ""
    typ = ""
    for sev_tok in SEVERITIES:
        m = re.search(rf"\b{sev_tok}\b", chunk)
        if m:
            sev = sev_tok
            tail = chunk[m.end():].strip().splitlines()
            if tail:
                # strip subsequent boilerplate
                typ_line = tail[0].strip()
                typ = typ_line.split("\n")[0].strip()
            break
    return sev, typ


CAUSE_RE = re.compile(
    r"(?m)^\s*(?:l\s+)?Cause\s+(\d+)\s*:\s*(.+?)(?=^\s*(?:l\s+)?Cause\s+\d+\s*:|\Z)",
    re.DOTALL,
)


def _split_causes(chunk: str) -> list[str]:
    out = []
    for m in CAUSE_RE.finditer(chunk):
        text = m.group(2).strip()
        text = re.sub(r"\s+", " ", text)
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# Section 1.7 — Alarm-suppression edges (hand-curated)
# ---------------------------------------------------------------------------

SUPPRESSION_EDGES: list[tuple[str, str]] = [
    # OTN optical layer (Fig 1-20)
    ("OTS_LOS", "OTS_LOS_P"),
    ("OTS_LOS", "OTS_LOS_O"),
    ("OTS_LOS", "OMS_SSF"),
    ("OTS_TIM", "OTS_BDI"),
    ("OTS_BDI", "OTS_BDI_O"),
    ("OTS_BDI", "OTS_BDI_P"),
    ("OMS_SSF", "OMS_SSF_P"),
    ("OMS_SSF", "OMS_SSF_O"),
    ("OMS_SSF", "OMS_FDI"),
    ("OMS_FDI", "OMS_FDI_O"),
    ("OMS_FDI", "OMS_FDI_P"),
    ("OMS_BDI", "OMS_BDI_O"),
    ("OMS_BDI", "OMS_BDI_P"),
    ("OMS_SSF", "OCh_SSF"),
    ("OCh_SSF", "OCh_SSF_P"),
    ("OCh_SSF", "OCh_SSF_O"),
    ("OCh_SSF", "OCh_FDI"),
    ("OCh_FDI", "OCh_FDI_P"),
    ("OCh_FDI", "OCh_FDI_O"),
    ("OCh_LOS_P", "R_LOS"),
    ("OCh_OCI",   "R_LOS"),
    ("R_LOS",     "OTUk_SSF"),
    # OTN electrical layer (Fig 1-21)
    ("OCH_LOS",   "OTUk_SSF"),
    ("LOL",       "OTUk_SSF"),
    ("OTUk_SSF",  "OTUk_LOF"),
    ("OTUk_LOF",  "OTUk_LOM"),
    ("OTUk_LOM",  "OTUk_TIM"),
    ("OTUk_TIM",  "OTUk_BDI"),
    ("OTUk_TIM",  "OTUk_DEG"),
    ("OTUk_LOM",  "ODUk_LOFLOM"),
    ("OPUk_PLM",  "ODUk_PM_BDI"),
    ("OPUk_PLM",  "ODUk_PM_DEG"),
    ("OPUk_MSIM", "ODUk_PM_BDI"),
    ("ODUk_PM_TIM","ODUk_PM_BDI"),
    ("ODUk_PM_TIM","ODUk_PM_DEG"),
    ("ODUk_TCMn_LTC","ODUk_TCMn_BDI"),
    ("ODUk_TCMn_LCK","ODUk_TCMn_BDI"),
    ("ODUk_TCMn_OCI","ODUk_TCMn_BDI"),
    ("ODUk_TCMn_TIM","ODUk_TCMn_BDI"),
    ("ODUk_TCMn_TIM","ODUk_TCMn_DEG"),
    # SDH (Fig 1-23)
    ("R_LOS", "R_LOF"),
    ("R_LOF", "MS_AIS"),
    ("MS_AIS", "AU_AIS"),
    ("MS_AIS", "B2_EXC"),
    ("B2_EXC", "B1_EXC"),
    ("AU_AIS", "AU_LOP"),
    ("AU_LOP", "HP_TIM"),
    ("AU_LOP", "HP_SLM"),
    ("AU_LOP", "HP_UNEQ"),
    ("AU_LOP", "B3_EXC"),
]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Reading {SRC_TXT}")
    text = load_text(SRC_TXT)
    pages = split_pages(text)
    print(f"Loaded {len(pages)} pages")

    print("Parsing index (Section 2.1)...")
    idx = parse_index(pages)
    print(f"  index entries: {len(idx)}")

    print("Parsing detail blocks (Section 3+)...")
    detail = parse_details(pages)
    print(f"  detail entries: {len(detail)}")

    # write outputs
    out_index = os.path.join(DATA, "alarms_index.json")
    out_detail = os.path.join(DATA, "alarms_detail.json")
    out_supp = os.path.join(DATA, "alarm_suppression.json")
    with open(out_index, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False)
    with open(out_detail, "w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in detail.items()},
                  f, indent=2, ensure_ascii=False)
    with open(out_supp, "w", encoding="utf-8") as f:
        json.dump(SUPPRESSION_EDGES, f, indent=2)

    # quick stats
    sev_counts: dict[str, int] = {}
    for v in idx.values():
        sev_counts[v["severity"]] = sev_counts.get(v["severity"], 0) + 1
    print(f"\nWrote {out_index}  ({sum(sev_counts.values())} alarms)")
    print(f"Wrote {out_detail}")
    print(f"Wrote {out_supp}  ({len(SUPPRESSION_EDGES)} edges)")
    print("Severity distribution:", sev_counts)


if __name__ == "__main__":
    main()
