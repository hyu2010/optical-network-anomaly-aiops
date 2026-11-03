"""yangson validation of outgoing payloads (doc 06 §9).

The YANG library is ietf-incident (extracted from the draft in references/),
the two diag-agent modules, and pyang's bundled IETF modules.  The full
diag-agent-alarm-types module (one identity per catalogue alarm) is written
by `diag-agent kb build` to data/diag_agent/yang/; the packaged excerpt is
the fallback.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

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

NODE_PATHS = {
    "incident-notification": "/ietf-incident:incident-notification",
    "diagnosis-task-notification": "/diag-agent-incident-ext:diagnosis-task-notification",
    "diagnose-input": "/ietf-incident:incident-diagnose/input",
    "diagnose-output": "/ietf-incident:incident-diagnose/output",
    "acknowledge-input": "/ietf-incident:incident-acknowledge/input",
    "resolve-input": "/ietf-incident:incident-resolve/input",
}


class ValidationFailed(ValueError):
    pass


def extract_ietf_incident(draft: Path, dst: Path) -> None:
    lines = draft.read_text().split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("module ietf-incident {"))
    end = next(i for i in range(start, len(lines)) if "<CODE ENDS>" in lines[i])
    body = [l[3:] if l.startswith("   ") else l for l in lines[start:end]
            if not re.match(r"^(Hu, et al\.|Internet-Draft\s)", l) and "\f" not in l]
    dst.write_text("\n".join(body) + "\n")


class PayloadValidator:
    def __init__(self, repo_root: Path, alarm_types_yang: Path | None = None, policy: str = "debug"):
        from yangson import DataModel
        self.policy = policy
        self.n_checked = 0
        self._tmp = tempfile.TemporaryDirectory(prefix="diag-yang-")
        tmp = Path(self._tmp.name)
        bundled = Path(sys.prefix) / "share" / "yang" / "modules"
        draft = repo_root / "references" / "draft-ietf-nmop-network-incident-yang-20.txt"
        for name, (rev, _, _) in MODULES.items():
            dst = tmp / f"{name}@{rev}.yang"
            if name == "ietf-incident":
                extract_ietf_incident(draft, dst)
            elif name == "diag-agent-alarm-types" and alarm_types_yang and alarm_types_yang.exists():
                shutil.copy(alarm_types_yang, dst)
            elif name.startswith("diag-agent"):
                shutil.copy(HERE / "yang" / f"{name}.yang", dst)
            else:
                shutil.copy(next(bundled.rglob(f"{name}.yang")), dst)
        library = {"ietf-yang-library:modules-state": {"module-set-id": "diag-agent", "module": [
            {"name": n, "revision": r, "namespace": ns, "conformance-type": c} for n, (r, ns, c) in MODULES.items()]}}
        self.dm = DataModel(json.dumps(library), [str(tmp)])

    def _should(self) -> bool:
        if self.policy in ("always", "debug"):
            return True
        if self.policy == "sample":
            return random.random() < 0.01
        return False

    def data_tree(self, payload: dict, force: bool = False) -> None:
        if not (force or self._should()):
            return
        from yangson.enumerations import ContentType
        try:
            self.dm.from_raw(payload).validate(ctype=ContentType.all)
        except Exception as e:  # noqa: BLE001
            raise ValidationFailed(f"incidents data tree: {type(e).__name__}: {e}") from None
        self.n_checked += 1

    def node(self, kind: str, payload: dict, force: bool = False) -> None:
        if not (force or self._should()):
            return
        try:
            self.dm.get_schema_node(NODE_PATHS[kind]).from_raw(next(iter(payload.values())))
        except Exception as e:  # noqa: BLE001
            raise ValidationFailed(f"{kind}: {type(e).__name__}: {e}") from None
        self.n_checked += 1
