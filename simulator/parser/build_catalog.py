"""Merge index + detail into a single flat alarm_catalog.json.

For names with both OCS / OTN variants, we keep them as separate keys
(`R_LOS::OCS`, `R_LOS::OTN`) but also publish the union under the bare
name with merged causes.
"""
from __future__ import annotations

import json
import os
import re

DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))


def main() -> None:
    idx = json.load(open(os.path.join(DATA, "alarms_index.json"), encoding="utf-8"))
    det = json.load(open(os.path.join(DATA, "alarms_detail.json"), encoding="utf-8"))

    catalog: dict[str, dict] = {}
    for name, meta in idx.items():
        catalog[name] = dict(meta)
        catalog[name]["possible_causes"] = []
        catalog[name]["impact"] = ""
        catalog[name]["alarm_type"] = ""

    for key, d in det.items():
        # key looks like "NAME" or "NAME::variant"
        name = key.split("::", 1)[0]
        entry = catalog.setdefault(name, {
            "name": name,
            "description": d.get("description", ""),
            "severity": d.get("severity", ""),
            "service_affecting": False,
            "possible_causes": [],
            "impact": "",
            "alarm_type": "",
        })
        # union of causes
        for c in d.get("possible_causes", []):
            c_clean = re.sub(r"\s+", " ", c).strip()
            if c_clean and c_clean not in entry["possible_causes"]:
                entry["possible_causes"].append(c_clean)
        if not entry["impact"] and d.get("impact"):
            entry["impact"] = d["impact"][:1000]
        if not entry["alarm_type"] and d.get("alarm_type"):
            entry["alarm_type"] = d["alarm_type"]
        if not entry["severity"] and d.get("severity"):
            entry["severity"] = d["severity"]

    out = os.path.join(DATA, "alarm_catalog.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    n_total = len(catalog)
    n_causes = sum(1 for v in catalog.values() if v["possible_causes"])
    print(f"wrote {out}")
    print(f"  alarms: {n_total}")
    print(f"  with causes: {n_causes}")
    print(f"  total causes: {sum(len(v['possible_causes']) for v in catalog.values())}")


if __name__ == "__main__":
    main()
