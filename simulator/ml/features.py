"""Feature encoders + vocabulary discovery for the GNN training pipeline.

Design/11 §4 (node features) + §11 (schema pin).

This module owns the *categorical look-up tables* the rest of the pipeline
needs to turn raw KG rows into numeric tensors:

  * ``Vocab``        — a generic name -> integer-index map (one per categorical
                       feature: alarm name, layer, severity, fault type).
  * ``TopoVocab``    — the same idea, but grouped by topology entity *type*
                       (``ne``/``span``/``port``/...) so each type gets its own
                       ``nn.Embedding`` table (Design/11 §4.2).
  * ``FeatureBundle``— the single object that bundles every vocabulary + the
                       schema signature; it is JSON-cached next to the dataset
                       so a model checkpoint can reload the exact vocab it was
                       trained with.
  * ``build_alarm_features`` — the per-alarm dense feature vector (Design/11 §4.1).

Why discovery instead of hard-coding?  The original doc assumed "7 layers /
10 fault types", but the live data has **13** layers (``OCH/ODU/OMS/OTS/OTU/
SDH`` plus ``auto-power/client/clock/environment/equipment/other/protection``),
**11** atomic fault types (plus compound ones like
``fan_failure+fiber_attenuation_high``), and extra topology prefixes
(``fpos``, ``svc``).  Discovering these from the data keeps the trainer correct
as the catalog grows and matches the §11 intent — the loader pins a schema
signature and raises on drift rather than silently mis-encoding a new column.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file so the package is import-safe from
# anywhere — ``simulator.ml`` lives two levels below the repo root).
# ---------------------------------------------------------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_KG_DIR = os.path.join(ROOT, "data", "kg")
DEFAULT_DB = os.path.join(ROOT, "data", "incidents.db")

# Design/11 §3.2 — the predicate vocabulary, split into INPUT (edges the GNN is
# allowed to see) vs TARGET (edges that are held out and used only as labels).
#
# The trainer asserts the on-disk predicate set matches ``schema_signature`` so
# that a schema change (a new predicate or entity type) fails loudly instead of
# silently mis-encoding the graph (§11).

# Static topology structure: containment, fiber adjacency, what rides on what.
# These define the shared "spine" that is identical in every incident's graph.
INPUT_TOPO_PREDICATES = {
    "Hosts", "Connected", "CarriedBy", "RidesOver", "SupportingTunnel", "Carries", "OnSpan",
}
# Per-alarm location anchors: each alarm points at where it was raised.
INPUT_ANCHOR_PREDICATES = {
    "IsLayer", "AtNE", "AtBoard", "AtPort", "AtSpan", "OnPipe", "AtChannel", "Container",
}
# Pre-computed pairwise alarm proximity (locality) — alarm <-> alarm edges.
INPUT_LOCALITY_PREDICATES = {
    "OnSameNE", "OnSameBoard", "OnSamePort", "OnSameContainer", "SameSpan", "OnSameLink",
}
# Graph-level temporal flags. These are NOT edges in the GNN input — they are
# flattened into the per-incident super-node's feature vector (§3.3).
INPUT_TEMPORAL_PREDICATES = {"AllClose", "AllWithinSix"}
# Held-out labels (never fed as input edges — see §3.2 target table):
TARGET_PREDICATES = {"RootCause", "Correlated", "Secondary"}
# The Correlated predicate is special: in the "rule-augmented" regime (§5.5) the
# rule:R* variant may be fed as input; otherwise it is a held-out label.
OPTIONAL_RULE_INPUT = {"Correlated"}

# Union of every predicate this code knows how to encode. Used to build the
# schema signature (§11) and to detect drift.
ALL_KNOWN_PREDICATES = (
    INPUT_TOPO_PREDICATES | INPUT_ANCHOR_PREDICATES | INPUT_LOCALITY_PREDICATES
    | INPUT_TEMPORAL_PREDICATES | TARGET_PREDICATES
)


# Stable node-type name constants. dataset.py / model.py key HeteroData on
# these strings, so a typo here would silently break message passing.
ALARM_NODE_TYPE = "alarm"           # the only node type that gets a head / label
INCIDENT_NODE_TYPE = "incident"     # the per-incident super-node (§3.3)
LAYER_NODE_TYPE = "layer"           # layer:X nodes (global, like other topology)


# ===========================================================================
# Vocab — a name -> index map for one categorical feature
# ===========================================================================

@dataclass
class Vocab:
    """An insertion-ordered, deduplicating map from string -> integer index.

    Used for every categorical feature: alarm names, layers, severities, fault
    types.  Index 0 is reserved as the "unknown" slot (``index(..., unknown=0)``)
    so an unseen value at inference maps to a real row rather than crashing.
    """

    items: list[str] = field(default_factory=list)        # index -> name (ordered)
    _idx: dict[str, int] = field(default_factory=dict)    # name -> index (fast lookup)

    def fit(self, values) -> "Vocab":
        """Add every unseen value in ``values`` to the vocabulary, in order.

        Idempotent: re-fitting already-seen values is a no-op. Returns ``self``
        so it chains (``Vocab().fit([...).index(...)``).
        """
        seen = set()
        for v in values:
            if v is None:
                continue
            s = str(v)
            if s == "" or s in seen:
                continue
            seen.add(s)
            self._idx[s] = len(self.items)   # next index = current length
            self.items.append(s)
        return self

    def index(self, value: Any, unknown: int = 0) -> int:
        """Look up the integer index for ``value``; ``unknown`` if not present."""
        return self._idx.get(str(value), unknown)

    def __len__(self) -> int:
        return len(self.items)

    # -- JSON (de)serialisation so a checkpoint can reload its exact vocab --
    def to_json(self) -> dict:
        return {"items": self.items}

    @classmethod
    def from_json(cls, d: dict) -> "Vocab":
        v = cls()
        v.fit(d.get("items", []))
        return v


# ===========================================================================
# TopoVocab — per-type URI -> index maps for topology nodes
# ===========================================================================

@dataclass
class TopoVocab:
    """Topology-node vocabularies, grouped by entity type.

    Each type (``ne``, ``span``, ``port``, ``board``, ``och``, ``otu``,
    ``odu``, ``layer``, ...) gets its own ``Vocab``.  Because the lab mesh is
    fixed, the *same* NE / span / port keeps the same index in every incident's
    graph, so the model's ``nn.Embedding`` gives it one shared vector
    (Design/11 §4.2 — "the model can learn that span_OMS3_BC is a frequent
    culprit").
    """

    by_type: dict[str, Vocab] = field(default_factory=dict)

    def fit_type(self, type_name: str, uris) -> None:
        """Register/extend the vocab for one topology type (e.g. ``"span"``)."""
        if type_name not in self.by_type:
            self.by_type[type_name] = Vocab().fit(uris)
        else:
            self.by_type[type_name].fit(uris)

    def index(self, uri: str, unknown: int = 0) -> tuple[str, int] | None:
        """Resolve a topology URI to ``(type, index)``.

        Returns ``None`` if the URI is not a known topology node (e.g. it is an
        ``alarm:`` URI, or an unrecognised prefix).  The caller uses ``None`` to
        decide whether an edge endpoint is topology or alarm.
        """
        if ":" not in uri:
            return None
        prefix = uri.split(":", 1)[0]          # e.g. "ne" in "ne:A"
        v = self.by_type.get(prefix)
        if v is None:
            return None
        return prefix, v.index(uri, unknown=unknown)

    # -- JSON (de)serialisation --
    def to_json(self) -> dict:
        return {k: v.to_json() for k, v in self.by_type.items()}

    @classmethod
    def from_json(cls, d: dict) -> "TopoVocab":
        tv = cls()
        for k, vd in d.items():
            tv.by_type[k] = Vocab.from_json(vd)
        return tv


def _uri_type(uri: str) -> str | None:
    """Return the prefix of a ``prefix:rest`` URI, or ``None`` if it has no ``:``."""
    return uri.split(":", 1)[0] if isinstance(uri, str) and ":" in uri else None


# ===========================================================================
# FeatureBundle — everything dataset.py + model.py need to encode nodes
# ===========================================================================

@dataclass
class FeatureBundle:
    """All vocabularies + the schema signature, in one JSON-serialisable object.

    Built once from ``data/kg/*.csv`` + ``incidents.db`` and cached alongside
    the dataset (``data/ml/cache/features.json``) so that:

      * dataset.py can encode nodes consistently across incidents;
      * a model checkpoint can reload the *exact* vocab it was trained with
        (so a re-fit of the vocab after a catalog change never silently
        mismatches a saved model).
    """

    alarm_names: Vocab = field(default_factory=Vocab)        # ~95 alarm names
    layers: Vocab = field(default_factory=Vocab)             # ~13 layers (discovered)
    severities: Vocab = field(default_factory=Vocab)         # 4 severity levels
    fault_types: Vocab = field(default_factory=Vocab)        # atomic fault labels
    topo: TopoVocab = field(default_factory=TopoVocab)       # per-type topology URIs
    # NOTE: the embedding *dimension* (name_dim etc.) is a model hyperparameter
    # and lives in model.py, not here — here we only store the vocab *sizes*.

    def dim_alarm_feature(self, name_dim: int = 32) -> int:
        """Width of the per-alarm dense feature vector (§4.1).

        Layout: ``[name_emb(name_dim) | layer_onehot | sev_onehot | dt | is_first]``.
        The model owns the ``nn.Embedding`` for the name; the dense part is the
        rest, which is what this returns (used to size the backbone's input).
        """
        return name_dim + len(self.layers) + len(self.severities) + 1 + 1

    @property
    def schema_signature(self) -> str:
        """A hashable summary of the predicate vocabulary + fault labels (§11).

        dataset.py / model.py assert this matches what they were built against,
        so a schema change (new predicate / entity type / fault class) fails
        loudly instead of silently mis-encoding the graph.
        """
        return "|".join(sorted(ALL_KNOWN_PREDICATES)) \
             + "||faults=" + ",".join(self.fault_types.items)

    def to_json(self) -> dict:
        return {
            "alarm_names": self.alarm_names.to_json(),
            "layers": self.layers.to_json(),
            "severities": self.severities.to_json(),
            "fault_types": self.fault_types.to_json(),
            "topo": self.topo.to_json(),
            "schema_signature": self.schema_signature,
        }

    @classmethod
    def from_json(cls, d: dict) -> "FeatureBundle":
        fb = cls()
        fb.alarm_names = Vocab.from_json(d["alarm_names"])
        fb.layers = Vocab.from_json(d["layers"])
        fb.severities = Vocab.from_json(d["severities"])
        fb.fault_types = Vocab.from_json(d["fault_types"])
        fb.topo = TopoVocab.from_json(d["topo"])
        return fb


# ===========================================================================
# Alarm feature vector (Design/11 §4.1)
# ===========================================================================

# Canonical severity ordering (Critical is "worst"). Discovery may yield these
# in any order from the DB; we re-fit in this order so one-hot column 0 always
# means Critical — important for consistent model input across runs.
SEVERITY_ORDER = ["Critical", "Major", "Minor", "Warning"]


def build_alarm_features(
    alarms_df: pd.DataFrame,
    fb: FeatureBundle,
    name_dim: int = 32,
) -> tuple[pd.DataFrame, "torch.Tensor"]:
    """Build the per-alarm dense feature matrix for one incident.

    Parameters
    ----------
    alarms_df : one row per alarm in the incident, with columns
        ``alarm_name, layer, severity, t_observed_ms`` (joined from
        ``incidents.db`` — see ``dataset.build_incident_graph``).
    fb : the vocabularies (layer / severity one-hot widths come from here).
    name_dim : the alarm-name embedding width (a model hyperparameter; only
        affects ``dim_alarm_feature``, the lookup index is returned separately).

    Returns
    -------
    (enriched_df, dense_tensor)
        * ``enriched_df`` — the input rows plus the integer encodings
          (``name_idx``, ``dt``, ``is_first``) so the caller can hand the
          name index to the model's ``nn.Embedding``.
        * ``dense_tensor`` — the concatenated dense vector
          ``[layer_onehot | sev_onehot | dt | is_first]`` the backbone consumes.
          (The name is *not* in here — it is embedded separately from name_idx.)

    Design/11 §4.1: name lookup + layer/severity one-hot + normalised observed
    time + is_first flag.  **Uses ``t_observed_ms``, never ``t_offset_ms``**
    (the latter is the simulator's internal causal time and creates an
    "earliest = root" shortcut — §10 Risk #3).
    """
    import torch  # local import: features.py stays importable without torch
    #   (e.g. to read a cached vocab in a process that has no torch installed).

    n = len(alarms_df)

    # (1) alarm-name -> vocab index. The model embeds this; we only need the int.
    name_idx = torch.tensor(
        [fb.alarm_names.index(nm, unknown=0) for nm in alarms_df["alarm_name"]],
        dtype=torch.long,
    )

    # (2) layer one-hot over the discovered layer vocab.
    nL, nS = len(fb.layers), len(fb.severities)
    layer_oh = torch.zeros(n, nL)
    for i, ly in enumerate(alarms_df["layer"]):
        layer_oh[i, fb.layers.index(ly, unknown=0)] = 1.0
    # (3) severity one-hot (column 0 == Critical by construction — see above).
    sev_oh = torch.zeros(n, nS)
    for i, sv in enumerate(alarms_df["severity"]):
        sev_oh[i, fb.severities.index(sv, unknown=0)] = 1.0

    # (4) normalised observed time Δt in [0,1], relative to the incident's own
    #     [min, max] window. ``or 1.0`` guards the single-alarm (zero-span) case.
    t = alarms_df["t_observed_ms"].astype(float).to_numpy()
    tmin = t.min() if t.size else 0.0
    span = (t.max() - tmin) or 1.0
    dt = torch.tensor((t - tmin) / span, dtype=torch.float).clamp(0.0, 1.0).unsqueeze(1)
    # (5) is_first: a noise-robust "earliest" signal (1 for the min-t_observed
    #     alarm). The plan keeps it as a feature despite the shortcut risk,
    #     with an ablation to verify the model doesn't rely on it alone.
    is_first = torch.tensor((t == tmin).astype(float), dtype=torch.float).unsqueeze(1)

    # Dense concatenation. NOTE: the name embedding is intentionally NOT here —
    # the model owns the nn.Embedding and looks it up from ``name_idx`` below.
    dense = torch.cat([layer_oh, sev_oh, dt, is_first], dim=1)

    # Return the integer encodings alongside, so the caller can store name_idx
    # on the HeteroData node block for the model to embed.
    enriched = alarms_df.reset_index(drop=True).copy()
    enriched["name_idx"] = name_idx.tolist()
    enriched["dt"] = dt.squeeze(1).tolist()
    enriched["is_first"] = is_first.squeeze(1).tolist()
    return enriched, dense


# ===========================================================================
# Vocabulary discovery — the build step
# ===========================================================================

def build_feature_bundle(
    kg_dir: str = DEFAULT_KG_DIR,
    db_path: str = DEFAULT_DB,
) -> FeatureBundle:
    """Scan ``data/kg/*.csv`` + ``incidents.db`` and discover every vocabulary.

    Idempotent and deterministic. The result is meant to be cached via
    ``save_feature_bundle`` so model checkpoints reload the exact vocab.

    Order of discovery matters only for *stability across runs* (the same data
    always yields the same indices); severity is additionally re-ordered to the
    canonical ``SEVERITY_ORDER`` so one-hot column 0 == Critical everywhere.
    """
    fb = FeatureBundle()
    alarm_csv = pd.read_csv(os.path.join(kg_dir, "alarm_triples.csv"))
    topo_csv = pd.read_csv(os.path.join(kg_dir, "topology_triples.csv"))
    manifest = pd.read_csv(os.path.join(kg_dir, "incident_manifest.csv"))

    # ---- alarm-name vocab: from the KG's alarm-sourced rows (subject_name) ----
    fb.alarm_names.fit(
        alarm_csv[alarm_csv["source"] == "alarm"]["subject_name"].dropna()
    )

    # ---- layers + severities: incidents.db is authoritative (it has the full
    #      set including layers that never appear as an IsLayer object); fall
    # back to nothing if the DB is absent (e.g. a pure-CSV test fixture).
    db_layers: list[str] = []
    db_sevs: list[str] = []
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        db_layers = [r[0] for r in conn.execute("SELECT DISTINCT layer FROM alarms") if r[0]]
        db_sevs = [r[0] for r in conn.execute("SELECT DISTINCT severity FROM alarms") if r[0]]
        conn.close()

    # IsLayer objects in the KG are URIs like ``layer:OMS`` — strip the prefix so
    # they canonicalise to the SAME bare name the DB uses. Merge DB + KG sources
    # and re-fit ONCE (Vocab.fit dedups only within a single call, so accumulating
    # across two .fit() calls would double-count "OMS" and "layer:OMS").
    kg_layers = []
    for o in alarm_csv[alarm_csv["predicate"] == "IsLayer"]["object"].dropna():
        s = str(o)
        kg_layers.append(s.split(":", 1)[1] if s.startswith("layer:") else s)
    fb.layers = Vocab().fit(sorted(set(db_layers) | set(kg_layers)))

    # Re-fit severities in canonical order so one-hot column 0 == Critical,
    # regardless of the (arbitrary) discovery order from the DB.
    fb.severities = Vocab().fit(
        [s for s in SEVERITY_ORDER if any(str(x) == s for x in db_sevs)]
        + sorted(set(db_sevs) - set(SEVERITY_ORDER))
    )

    # ---- register `layer:X` as topology nodes ----
    # layer nodes are global/shared across incidents just like NEs and spans,
    # so they must live in the topo vocab (type "layer") — otherwise the
    # IsLayer anchor edges have no global index for their endpoint and get
    # silently dropped. (This was a real bug during ML-1; see git history.)
    fb.topo.fit_type(LAYER_NODE_TYPE, [f"layer:{ly}" for ly in fb.layers.items])

    # ---- fault types: split compound faults on '+' so the aux fault-class head
    #      (§5.4) classifies atomic types. e.g. "fan_failure+fiber_attenuation_high"
    #      contributes both tokens. The PRIMARY label used per incident is the
    #      first atomic token (see dataset.build_incident_graph).
    atomic = set()
    for s in manifest["fault_type"].dropna():
        for tok in str(s).split("+"):
            tok = tok.strip()
            if tok:
                atomic.add(tok)
    fb.fault_types = Vocab().fit(sorted(atomic))

    # ---- topology URIs -> per-type vocabs (ne / board / port / span / och /
    #      otu / odu / fpos / svc / ...). Group every URI in topology_triples
    #      by its prefix and fit one Vocab per type.
    topo_uris = pd.concat([topo_csv["subject"], topo_csv["object"]]).dropna().unique()
    by_type: dict[str, list[str]] = {}
    for uri in topo_uris:
        t = _uri_type(uri)
        if t and not uri.startswith("alarm:"):   # alarms are not topology
            by_type.setdefault(t, []).append(uri)
    for t, uris in sorted(by_type.items()):       # sorted for run-to-run stability
        fb.topo.fit_type(t, uris)

    return fb


def save_feature_bundle(fb: FeatureBundle, path: str) -> None:
    """Write the feature bundle (vocabularies + schema signature) to JSON."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fb.to_json(), f, indent=1)


def load_feature_bundle(path: str) -> FeatureBundle:
    """Reload a feature bundle saved by :func:`save_feature_bundle`."""
    with open(path, encoding="utf-8") as f:
        return FeatureBundle.from_json(json.load(f))
