"""Validate the ietf-incident JSON examples with yangson.

Usage:
    pip install yangson pyang
    python docs/design/examples/validate.py

The module directory is built from:
  - ietf-incident, extracted from references/draft-ietf-nmop-network-incident-yang-20.txt
  - diag-agent-incident-ext and diag-agent-alarm-types (docs/design/yang/)
  - ietf-alarms, ietf-network, ietf-yang-types, ietf-inet-types,
    ietf-yang-structure-ext, as installed by pip with pyang
    (<sys.prefix>/share/yang/modules)

Data trees (the incidents container) get full validation, including when,
leafref and mandatory checks.  Notifications and RPC bodies are parsed
against their schema nodes, which checks types and identities.  yangson
does not enforce decimal64 fraction-digits, so the payload mapper
quantises metric values to 2 digits itself.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from yangson import DataModel
from yangson.enumerations import ContentType

HERE = Path(__file__).resolve().parent
DESIGN = HERE.parent
REPO = DESIGN.parent.parent
DRAFT = REPO / "references" / "draft-ietf-nmop-network-incident-yang-20.txt"

MODULES = {
    "ietf-alarms": ("2019-09-11", "urn:ietf:params:xml:ns:yang:ietf-alarms", "implement"),
    "ietf-yang-types": ("2013-07-15", "urn:ietf:params:xml:ns:yang:ietf-yang-types", "import"),
    "ietf-inet-types": ("2013-07-15", "urn:ietf:params:xml:ns:yang:ietf-inet-types", "import"),
    "ietf-network": ("2018-02-26", "urn:ietf:params:xml:ns:yang:ietf-network", "implement"),
    "ietf-yang-structure-ext": ("2020-06-17", "urn:ietf:params:xml:ns:yang:ietf-yang-structure-ext", "import"),
    "ietf-incident": ("2026-07-30", "urn:ietf:params:xml:ns:yang:ietf-incident", "implement"),
    "diag-agent-incident-ext": ("2026-11-02", "urn:example:yang:diag-agent-incident-ext", "implement"),
    "diag-agent-alarm-types": ("2026-11-02", "urn:example:yang:diag-agent-alarm-types", "implement"),
}

DATA = ["07-incidents-strict.json", "08-incidents-extended.json"]
NODES = [
    ("01-notification-raised.json", "/ietf-incident:incident-notification"),
    ("03-notification-updated-diagnosed.json", "/ietf-incident:incident-notification"),
    ("04-notification-updated-acknowledged.json", "/ietf-incident:incident-notification"),
    ("06-notification-cleared.json", "/ietf-incident:incident-notification"),
    ("09-diagnosis-task-notification.json", "/diag-agent-incident-ext:diagnosis-task-notification"),
    ("02-rpc-diagnose-request.json", "/ietf-incident:incident-diagnose/input"),
    ("02-rpc-diagnose-response.json", "/ietf-incident:incident-diagnose/output"),
    ("04-rpc-acknowledge-request.json", "/ietf-incident:incident-acknowledge/input"),
    ("05-rpc-resolve-request.json", "/ietf-incident:incident-resolve/input"),
]


def extract_ietf_incident(dst: Path) -> None:
    lines = DRAFT.read_text().split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("module ietf-incident {"))
    end = next(i for i in range(start, len(lines)) if "<CODE ENDS>" in lines[i])
    body = [
        l[3:] if l.startswith("   ") else l
        for l in lines[start:end]
        if not re.match(r"^(Hu, et al\.|Internet-Draft\s)", l) and "\f" not in l
    ]
    dst.write_text("\n".join(body) + "\n")


def build_module_dir(tmp: Path) -> None:
    bundled = Path(sys.prefix) / "share" / "yang" / "modules"
    for name, (rev, _, _) in MODULES.items():
        dst = tmp / f"{name}@{rev}.yang"
        if name == "ietf-incident":
            extract_ietf_incident(dst)
        elif name.startswith("diag-agent"):
            shutil.copy(DESIGN / "yang" / f"{name}.yang", dst)
        else:
            shutil.copy(next(bundled.rglob(f"{name}.yang")), dst)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        build_module_dir(tmp)
        library = {"ietf-yang-library:modules-state": {"module-set-id": "diag-agent", "module": [
            {"name": n, "revision": r, "namespace": ns, "conformance-type": c}
            for n, (r, ns, c) in MODULES.items()]}}
        dm = DataModel(json.dumps(library), [str(tmp)])
        failures = 0
        for name in DATA:
            try:
                dm.from_raw(json.loads((HERE / name).read_text())).validate(ctype=ContentType.all)
                print(f"valid   {name}")
            except Exception as exc:  # noqa: BLE001 - report every failure
                failures += 1
                print(f"INVALID {name}: {exc}")
        for name, path in NODES:
            raw = json.loads((HERE / name).read_text())
            try:
                dm.get_schema_node(path).from_raw(next(iter(raw.values())))
                print(f"parsed  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"INVALID {name}: {exc}")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
