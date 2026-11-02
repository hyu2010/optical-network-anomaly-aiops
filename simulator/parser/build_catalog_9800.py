"""Build a 9800-derived alarm catalog from Library/extracted/osn9800.txt.

The 9800 product-alarm-list (§1.2.3) has rows like:

    T_ALOS Indicates loss of analog signals at the E1 or T1 interfaces Major V100R020C10
    TU_AIS_VC3 An alarm indication signal (AIS) for a lower order VC-3 TU
    channel
    Major V100R002C10

i.e. NAME [description (possibly multi-line)] SEVERITY V<version>.

The §1.2.4 detail entries use `•` bullets in Possible Causes (vs `l` in 8800).

This script:
  1. Walks the alarm-list rows and pulls (name, description, severity, version)
  2. Walks the §1.2.4.<n> headings and pulls Possible Causes for those alarms
  3. Writes data/alarm_catalog_9800.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "Library", "extracted", "osn9800.txt")
OUT_CAT = os.path.join(ROOT, "data", "alarm_catalog_9800.json")
OUT_DETAIL = os.path.join(ROOT, "data", "alarms_detail_9800.json")

SEVERITIES = {"Critical", "Major", "Minor", "Warning"}

# Row-terminator: <severity> V<version>
TAIL_RE = re.compile(
    r"^(.*?)\s*(Critical|Major|Minor|Warning)\s+(V\d+R\d+C\d+)\s*$"
)
NAME_RE = re.compile(r"^([A-Z][A-Za-z0-9_]+)\s*(.*)$")
JUNK_RE = re.compile(
    r"^(?:"
    r"OptiX OSN.*|"
    r"\d{4}-\d{2}-\d{2}\s*\d+|"        # date + page footer
    r"Issue.*|"
    r"Copyright.*|"
    r"Huawei Proprietary.*|"
    r"Name Description.*|"
    r"Severity.*|"
    r"Affecting|"
    r"Version.*|"
    r"\d+|"
    r"Product Alarm List.*|"
    r"This topic.*|"
    r"This section.*"
    r")\s*$"
)


@dataclass
class Detail:
    name: str
    description: str = ""
    severity: str = ""
    alarm_type: str = ""
    impact: str = ""
    fault_symptom: str = ""
    possible_causes: list[str] = field(default_factory=list)


def split_pages(text: str) -> list[tuple[int, str]]:
    parts = re.split(r"\n=====\s*PAGE\s+(\d+)\s*=====\n", text)
    out = []
    for i in range(1, len(parts), 2):
        out.append((int(parts[i]), parts[i + 1]))
    return out


# ---------------------------------------------------------------------------
# Phase 1 — alarm list (§1.2.3)
# ---------------------------------------------------------------------------

def parse_index(pages: list[tuple[int, str]]) -> dict[str, dict]:
    """Walk §1.2.3 Product Alarm List and build {name: {description, severity, version}}."""
    in_section = False
    relevant: list[str] = []
    # find body pages (not TOC) — TOC ends ~ page 35; section 1.2.4 starts before page 175
    for pno, body in pages:
        if pno < 60:
            continue
        if "1.2.3 Product Alarm List" in body:
            in_section = True
        if in_section and "1.2.4 Clearing Common Alarms" in body:
            relevant.append(body)
            break
        if in_section:
            relevant.append(body)
    raw = "\n".join(relevant)
    lines = []
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
            sev  = tail.group(2)
            ver  = tail.group(3)
            if cur_name is None:
                # single-line row
                m = NAME_RE.match(head)
                if m:
                    name, desc = m.group(1), m.group(2).strip()
                    index[name] = {"name": name, "description": desc,
                                   "severity": sev, "version": ver}
            else:
                desc = " ".join(cur_desc + ([head] if head else [])).strip()
                index[cur_name] = {"name": cur_name, "description": desc,
                                   "severity": sev, "version": ver}
            cur_name = None
            cur_desc = []
            continue
        m = NAME_RE.match(ln)
        if m and cur_name is None:
            cur_name = m.group(1)
            rest = m.group(2).strip()
            cur_desc = [rest] if rest else []
        elif cur_name is not None:
            cur_desc.append(ln)
    return index


# ---------------------------------------------------------------------------
# Phase 2 — detail entries from §1.2.4 (Clearing Common Alarms)
# ---------------------------------------------------------------------------

DETAIL_HEAD_RE = re.compile(r"^1\.2\.4\.\d+(?:\.\d+)*\s+([A-Z][A-Za-z0-9_]+)\s*$",
                            re.MULTILINE)
FIELD_NAMES = ("Description", "Attribute", "Parameters", "Impact on the System",
               "Fault Symptom", "Possible Causes", "Procedure", "Related Information")
# bullets accept both `l` (8800) and `•` (9800)
CAUSE_RE = re.compile(
    r"(?m)^\s*(?:[l•]\s+)?Cause\s+\d+\s*[:.]?\s*(.+?)(?=^\s*(?:[l•]\s+)?Cause\s+\d+\s*[:.]?|\Z)",
    re.DOTALL,
)


def parse_details(text: str) -> dict[str, Detail]:
    """Walk §1.2.4 detail entries; return {name: Detail}."""
    out: dict[str, Detail] = {}
    headings = list(DETAIL_HEAD_RE.finditer(text))
    for i, m in enumerate(headings):
        name = m.group(1)
        body_start = m.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[body_start:body_end]
        if "Possible Causes" not in body[:6000]:
            continue
        d = _parse_one(name, body)
        out[name] = d
    return out


def _parse_one(name: str, body: str) -> Detail:
    d = Detail(name=name)
    positions = []
    for fld in FIELD_NAMES:
        for m in re.finditer(rf"(?m)^{re.escape(fld)}\s*$", body):
            positions.append((m.start(), fld))
    positions.sort()
    for idx, (pos, fld) in enumerate(positions):
        sec_start = pos + len(fld)
        sec_end = positions[idx + 1][0] if idx + 1 < len(positions) else len(body)
        chunk = body[sec_start:sec_end].strip()
        if fld == "Description":
            d.description = _clean(chunk)[:600]
        elif fld == "Attribute":
            for sev in SEVERITIES:
                if re.search(rf"\b{sev}\b", chunk):
                    d.severity = sev
                    tail = chunk.split(sev, 1)[1].strip().splitlines()
                    if tail:
                        d.alarm_type = tail[0].strip()
                    break
        elif fld == "Impact on the System":
            d.impact = _clean(chunk)[:400]
        elif fld == "Fault Symptom":
            d.fault_symptom = _clean(chunk)[:400]
        elif fld == "Possible Causes":
            d.possible_causes = _split_causes(chunk)
    return d


def _clean(s: str) -> str:
    s = re.sub(r"OptiX OSN [^\n]*\n", "", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}\s+\d+\n", "", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _split_causes(chunk: str) -> list[str]:
    out = []
    for m in CAUSE_RE.finditer(chunk):
        text = m.group(1).strip()
        text = re.sub(r"\s+", " ", text)
        out.append(text[:300])
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"Reading {SRC}")
    text = open(SRC, encoding="utf-8").read()
    pages = split_pages(text)
    print(f"  {len(pages)} pages")

    print("Parsing index (§1.2.3)...")
    idx = parse_index(pages)
    print(f"  index entries: {len(idx)}")

    print("Parsing detail blocks (§1.2.4)...")
    details = parse_details(text)
    print(f"  detail entries: {len(details)}")

    # Build a unified catalog
    catalog: dict[str, dict] = {}
    for name, meta in idx.items():
        catalog[name] = {
            "name": name,
            "description": meta["description"],
            "severity": meta["severity"],
            "version": meta.get("version", ""),
            "possible_causes": [],
            "impact": "",
            "alarm_type": "",
            "source": "OSN 9800",
        }
    for name, d in details.items():
        e = catalog.setdefault(name, {
            "name": name, "description": d.description, "severity": d.severity,
            "version": "", "possible_causes": [], "impact": "",
            "alarm_type": "", "source": "OSN 9800",
        })
        e["possible_causes"] = d.possible_causes
        if not e["impact"] and d.impact:
            e["impact"] = d.impact
        if not e["alarm_type"] and d.alarm_type:
            e["alarm_type"] = d.alarm_type
        if d.severity and not e["severity"]:
            e["severity"] = d.severity

    with open(OUT_CAT, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    with open(OUT_DETAIL, "w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in details.items()},
                  f, indent=2, ensure_ascii=False)

    n_with_causes = sum(1 for v in catalog.values() if v["possible_causes"])
    sev_counts = {}
    for v in catalog.values():
        s = v["severity"] or "(blank)"
        sev_counts[s] = sev_counts.get(s, 0) + 1
    print()
    print(f"Wrote {OUT_CAT}")
    print(f"      {OUT_DETAIL}")
    print(f"  catalog entries: {len(catalog)}")
    print(f"  with possible-causes: {n_with_causes}")
    print(f"  severity distribution: {sev_counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
