"""Per-incident graph construction: ``data/kg/*.csv`` + ``incidents.db`` -> PyG ``HeteroData``.

Design/11 §3.  For each ``incident_id`` we build ONE heterogeneous graph:

  * **alarm nodes**        — local index ``0..N-1``. Carry the dense feature
                             vector (§4.1), the ``name_idx`` for the model's
                             ``nn.Embedding``, the ``is_root`` label (the root
                             head's target, §5.2), and the correlation-pair
                             index (the correlation head's positives, §5.3).
  * **topology nodes**     — GLOBAL index. The same NE / span / port has the
                             same index in *every* incident's graph, so the
                             model's per-type ``nn.Embedding`` gives it one
                             shared vector (§4.2). Topology nodes are *never*
                             labelled and *never* get a head — they are context
                             that routes messages so each alarm embedding
                             "knows where it is" (see Design/11 §5.6).
  * **incident super-node** — one node per graph. Sinks the per-incident
                             temporal flags (``AllClose`` / ``AllWithinSix``)
                             and carries the optional fault-class label (§3.3).

Edges are typed ``(src_type, predicate, dst_type)``; the input-vs-label split
follows §3.2 (anchors/locality/topology are INPUT; ``RootCause``/``Secondary``
are labels; ``Correlated`` is a label in the pure-GNN regime and an input edge
in the rule-augmented regime).  Reverse edges are added so messages flow both
ways (§3.4).

**The root-label join.**  The alarm URI's last token IS the global
``alarms.id`` (verified: ``alarm:2:64`` <-> ``alarms.id=64``), so ``is_root`` is
looked up directly by id from ``incidents.db`` rather than parsed out of the
CSV.  This avoids the CSV having to carry the label (it doesn't — only the DB
does) and keeps the join a single indexed SELECT.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Iterable

import numpy as np
import pandas as pd
import torch
from torch_geometric.data import HeteroData

from .features import (
    ALARM_NODE_TYPE,
    DEFAULT_DB,
    DEFAULT_KG_DIR,
    FeatureBundle,
    INCIDENT_NODE_TYPE,
    INPUT_ANCHOR_PREDICATES,
    INPUT_LOCALITY_PREDICATES,
    INPUT_TEMPORAL_PREDICATES,
    INPUT_TOPO_PREDICATES,
    LAYER_NODE_TYPE,
    OPTIONAL_RULE_INPUT,
    TARGET_PREDICATES,
    build_alarm_features,
    build_feature_bundle,
)

# Matches "alarm:<incident_id>:<alarm_id>" and lets us pull the alarm_id group.
# The alarm_id is the global alarms.id (see module docstring).
ALARM_URI_RE = re.compile(r"alarm:(\d+):(\d+)")

# Predicates that become INPUT edges (union of the three §3.2 input tables:
# topology spine + alarm anchors + alarm<->alarm locality). Temporal predicates
# are excluded — they are flattened into the super-node, not made into edges.
INPUT_EDGE_PREDICATES = (
    INPUT_TOPO_PREDICATES | INPUT_ANCHOR_PREDICATES | INPUT_LOCALITY_PREDICATES
)


# ===========================================================================
# TopologyIndex — the shared spine, built ONCE
# ===========================================================================

class TopologyIndex:
    """The shared topology spine, built once from ``topology_triples.csv``.

    The topology graph is incident-independent (every row has
    ``incident_id == 0``), so it is identical across all per-incident graphs.
    Rather than re-parse it for each incident, we build the typed
    ``edge_index`` tensors once here and copy them verbatim into every
    ``HeteroData``.

    Each topology URI is resolved to a ``(type, global_index)`` pair via the
    feature bundle's ``TopoVocab`` — that global index is what makes NE "A" map
    to the same embedding row in every incident.
    """

    def __init__(self, topo_csv: pd.DataFrame, fb: FeatureBundle) -> None:
        self.fb = fb
        # Accumulate typed edge lists as Python lists first; converted to
        # tensors lazily by edge_tensors() (built once, reused per incident).
        # Key: (src_type, predicate, dst_type)  ->  list of (src_idx, dst_idx).
        self.edge_lists: dict[tuple[str, str, str], list[tuple[int, int]]] = {}

        for _, row in topo_csv.iterrows():
            s, p, o = row["subject"], row["predicate"], row["object"]
            st = fb.topo.index(s)   # (type, idx) or None
            dt = fb.topo.index(o)
            # Keep only topology<->topology edges in the spine. Any edge that
            # touches an alarm (there shouldn't be any in topology_triples.csv,
            # but guard anyway) is handled per-incident, not here.
            if st is None or dt is None:
                continue
            self.edge_lists.setdefault((st[0], p, dt[0]), []).append((st[1], dt[1]))

    def edge_tensors(self) -> dict[tuple[str, str, str], torch.Tensor]:
        """Materialise the accumulated edge lists as ``edge_index`` tensors.

        Each tensor is the PyG-standard ``[2, num_edges]`` ``long`` layout
        (row 0 = source indices, row 1 = destination indices).
        """
        out = {}
        for key, pairs in self.edge_lists.items():
            if not pairs:
                continue
            arr = torch.tensor(pairs, dtype=torch.long).t().contiguous()
            out[key] = arr
        return out

    def types_present(self) -> set[str]:
        """The set of topology types that appear in the spine (for sanity)."""
        return set(self.fb.topo.by_type.keys())


# ===========================================================================
# Per-incident graph construction
# ===========================================================================

def _alarm_id(uri: str) -> int | None:
    """Extract the global alarm id (== alarms.id) from an ``alarm:S:A`` URI."""
    m = ALARM_URI_RE.match(str(uri))
    return int(m.group(2)) if m else None


def _layer_nodes_for(alarms: pd.DataFrame) -> set[str]:
    """The set of ``layer:X`` URIs referenced by an incident's alarms.

    (Currently unused at call sites — kept for future per-incident subsetting
    of the layer nodes; today we attach the full global layer set, matching
    the other topology types.)
    """
    return {f"layer:{ly}" for ly in alarms["layer"].dropna().astype(str).unique()}


def build_incident_graph(
    incident_id: int,
    alarm_csv: pd.DataFrame,
    manifest: pd.DataFrame,
    topo_edges: dict[tuple[str, str, str], torch.Tensor],
    fb: FeatureBundle,
    db_path: str = DEFAULT_DB,
    with_rule_edges: bool = False,
    name_dim: int = 32,
) -> HeteroData:
    """Construct one ``HeteroData`` graph for a single incident.

    Parameters
    ----------
    incident_id : the incident to build (a row in ``incident_manifest.csv``).
    alarm_csv / manifest : the full KG tables (filtered to this incident inside).
    topo_edges : the precomputed shared topology spine (from
        ``TopologyIndex.edge_tensors()``) — copied in verbatim.
    fb : the vocabularies / encoders.
    db_path : path to ``incidents.db`` (for the ``is_root`` / feature join).
    with_rule_edges : regime toggle (Design/11 §5.5).  ``False`` (default) =
        **pure-GNN** regime: ``Correlated`` edges are held out as a label.
        ``True`` = **rule-augmented** regime: ``rule:R* Correlated`` edges are
        additionally fed as input, on top of topology + locality.
    name_dim : alarm-name embedding width (model hyperparameter; only flows
        through to ``build_alarm_features`` for sizing).

    Returns
    -------
    HeteroData with:
      * ``data['alarm']``        — x, name_idx, y_root, y_secondary, corr_pos, uri
      * ``data['incident']``     — x (temporal flags), y_fault
      * ``data[<topo type>]``    — global_id (one per topology type)
      * typed ``edge_index`` entries for every input predicate (+ ``_rev``)
    """
    inc_rows = alarm_csv[alarm_csv["incident_id"] == incident_id]
    if inc_rows.empty:
        raise ValueError(f"incident {incident_id} has no alarm triples")

    data = HeteroData()
    data.incident_id = int(incident_id)   # carried for debug / explanation

    # ------------------------------------------------------------------
    # (1) ALARM NODES — local index 0..N-1
    # ------------------------------------------------------------------
    # Distinct alarm URIs for this incident, sorted by their alarm_id so the
    # local index ordering is stable and human-predictable. Filter to rows whose
    # subject matches the alarm: URI scheme — the KG's temporal rows
    # (AllClose/AllWithinSix) use the incident super-node ``incident:N`` as
    # their subject, and without this filter that URI sneaks in as a ghost
    # "alarm" node at index 0 (blank features, wrongly labelled).
    alarm_uris = sorted(
        (u for u in inc_rows["subject"].drop_duplicates().tolist()
         if ALARM_URI_RE.match(str(u))),
        key=lambda u: _alarm_id(u) or 0,
    )
    alarm_local: dict[str, int] = {u: i for i, u in enumerate(alarm_uris)}
    n_alarm = len(alarm_uris)

    # ------------------------------------------------------------------
    # ROOT LABEL — read from the KG, not from incidents.db (Design/11 §2.2/§2.4
    # revised): incidents.db is a pure alarm dump with NO answer key (the
    # is_root column was removed), so the label comes from the KG's RootCause
    # triples. Provenance rule: prefer source=alarm (the simulator's ground
    # truth) when present; fall back to source=rule (the rule reasoner's pick —
    # what the current KG carries). NOTE: training on source=rule makes the GNN
    # a learned approximator of the rules; to compare against the rules
    # baseline, GT (source=alarm) must be restored into the KG.
    # ------------------------------------------------------------------
    rc = inc_rows[inc_rows["predicate"] == "RootCause"]
    gt = rc[rc["source"] == "alarm"] if "source" in rc.columns else rc.iloc[0:0]
    root_rows = gt if len(gt) else rc
    root_uris = set(root_rows["subject"].tolist())
    if not root_uris:
        # No label of any provenance — nothing to train/evaluate on. Skipped
        # uniformly by build_dataset(skip_empty=True).
        raise ValueError(f"incident {incident_id} has no RootCause label in the KG")

    # Join per-alarm FEATURE facts (name/severity/layer/timing) from incidents.db.
    # The URI's alarm_id IS alarms.id, so this is a direct lookup. (Features only
    # — never the label.)
    aids = [_alarm_id(u) for u in alarm_uris]
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, alarm_name, ne, board, port, severity, layer, t_observed_ms "
        "FROM alarms WHERE incident_id=? AND id IN (%s)" % ",".join("?" * len(aids)),
        (incident_id, *aids),
    ).fetchall()
    conn.close()
    by_id = {r[0]: r for r in rows}

    # Fall back to the CSV's subject_name if a row is missing in the DB (keeps
    # the loader working on a CSV-only fixture without the DB row).
    name_from_csv = {u: inc_rows[inc_rows["subject"] == u]["subject_name"].iloc[0]
                     for u in alarm_uris}

    recs = []
    y_root = torch.zeros(n_alarm, dtype=torch.long)   # the root-head label (§5.2)
    for i, (u, aid) in enumerate(zip(alarm_uris, aids)):
        r = by_id.get(aid)
        if r:
            _, name, ne, board, port, sev, layer, t_obs = r
        else:
            # Missing DB row: degrade gracefully (blank features).
            name = name_from_csv.get(u, "?"); ne = board = port = sev = layer = ""; t_obs = 0
        recs.append({"alarm_name": name, "ne": ne, "board": board, "port": port,
                     "severity": sev, "layer": layer, "t_observed_ms": t_obs or 0})
        y_root[i] = int(u in root_uris)   # 1 for every alarm in the (possibly
                                          # multi-root) root set, else 0

    # Build the dense feature matrix + integer encodings (§4.1).
    alarms_df = pd.DataFrame(recs)
    enriched, dense = build_alarm_features(alarms_df, fb, name_dim=name_dim)

    data[ALARM_NODE_TYPE].x = dense                                           # [N, dense_dim]
    data[ALARM_NODE_TYPE].name_idx = torch.tensor(enriched["name_idx"].tolist(), dtype=torch.long)
    data[ALARM_NODE_TYPE].y_root = y_root                                     # node label (§5.2)
    data[ALARM_NODE_TYPE].num_nodes = n_alarm
    data[ALARM_NODE_TYPE].uri = alarm_uris                                    # for debug / explain

    # ------------------------------------------------------------------
    # (2) TOPOLOGY NODES — global index, FULL shared set
    # ------------------------------------------------------------------
    # We attach the full global topology node set to every incident. It is small
    # and shared, and the model's per-type nn.Embedding(num_global, d) is shared
    # across incidents, so every incident carries the same topology node set —
    # consistent with §3.1's "topology nodes get the same index across all
    # incidents via a global table".
    for tname, vocab in fb.topo.by_type.items():
        data[tname].global_id = torch.arange(len(vocab), dtype=torch.long)
        data[tname].num_nodes = len(vocab)

    # ------------------------------------------------------------------
    # (3) INCIDENT SUPER-NODE (§3.3) — one node, graph-level context
    # ------------------------------------------------------------------
    # The temporal flags (AllClose / AllWithinSix) are edges in the CSV but we
    # flatten them into the super-node's 2-d feature vector instead of feeding
    # them as edges (they are graph-level booleans, not relations).
    temporal = inc_rows[inc_rows["predicate"].isin(INPUT_TEMPORAL_PREDICATES)]
    flags = {p: 0 for p in INPUT_TEMPORAL_PREDICATES}
    for p in temporal["predicate"]:
        flags[p] = 1

    # Fault-class label = the FIRST atomic token of the (possibly compound)
    # fault_type. e.g. "fan_failure+fiber_attenuation_high" -> "fan_failure".
    man_row = manifest[manifest["incident_id"] == incident_id]
    ft = str(man_row.iloc[0]["fault_type"]) if not man_row.empty else ""
    ft_label = fb.fault_types.index(ft.split("+")[0].strip(), unknown=0) if ft else 0

    data[INCIDENT_NODE_TYPE].x = torch.tensor(
        [[flags["AllClose"], flags["AllWithinSix"]]], dtype=torch.float)
    data[INCIDENT_NODE_TYPE].y_fault = torch.tensor([ft_label], dtype=torch.long)
    data[INCIDENT_NODE_TYPE].num_nodes = 1

    # ------------------------------------------------------------------
    # (4) EDGES
    # ------------------------------------------------------------------
    def resolve(uri: str):
        """Map a URI to ``(node_type, index)`` for either an alarm (local) or a
        topology node (global). Returns ``(None, None)`` if the URI is
        unrecognised (e.g. a literal like ``true``)."""
        if uri in alarm_local:
            return ALARM_NODE_TYPE, alarm_local[uri]
        t = fb.topo.index(uri)
        if t is not None:
            return t[0], t[1]
        return None, None

    # (4a) Topology spine — copy the precomputed shared tensors verbatim.
    for key, ei in topo_edges.items():
        data[key].edge_index = ei

    # (4b) Per-incident input edges (anchors + locality), plus label collection.
    # We accumulate per-relation edge lists in a buffer, then materialise them
    # (with reverse edges) after the loop.
    edge_buf: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    label_secondary: list[tuple[int, int]] = []  # (alarm_idx, 1) where a Secondary triple exists
    corr_pairs: set[tuple[int, int]] = set()     # positive alarm pairs (rule:R* Correlated)

    for _, row in inc_rows.iterrows():
        s, p, o = row["subject"], row["predicate"], row["object"]
        src_type, src = resolve(s)
        if src is None:
            continue   # subject we don't know how to place -> skip

        # --- TARGET predicates: capture as labels, do NOT add as input edges ---
        # RootCause is already captured via y_root (from the DB join above).
        if p == "RootCause":
            continue
        if p == "Secondary":
            if src_type == ALARM_NODE_TYPE:
                label_secondary.append((src, 1))
            continue
        # Temporal predicates were flattened into the super-node above.
        if p in INPUT_TEMPORAL_PREDICATES:
            continue

        # --- Correlated (rule:R*): label by default, input only if augmented ---
        if p == "Correlated":
            dt_type, dst = resolve(o)
            # Only alarm<->alarm correlation is a head target.
            if src_type == ALARM_NODE_TYPE and dt_type == ALARM_NODE_TYPE:
                i, j = sorted((src, dst))            # canonicalise pair ordering
                corr_pairs.add((i, j))
                if with_rule_edges:
                    # rule-augmented regime: ALSO feed it as an input edge.
                    edge_buf.setdefault((ALARM_NODE_TYPE, "Correlated", ALARM_NODE_TYPE), []).append((src, dst))
            continue

        # --- Plain INPUT predicates (anchors + locality + topology) ---
        if p in INPUT_EDGE_PREDICATES or (p == "Correlated" and with_rule_edges):
            dt_type, dst = resolve(o)
            if dt_type is None:
                continue
            edge_buf.setdefault((src_type, p, dt_type), []).append((src, dst))

    # Materialise the buffered edges + their reverse counterparts (§3.4).
    # Reverse edges get a distinct relation name ("<pred>_rev") so the R-GCN
    # gives them their own weight matrix — this matters for directional edges
    # like Connected (fibre direction) and AtNE (alarm->ne vs ne->alarm).
    for (st, p, dt), pairs in edge_buf.items():
        if not pairs:
            continue
        ei = torch.tensor(pairs, dtype=torch.long).t().contiguous()
        data[(st, p, dt)].edge_index = ei
        rev_key = (dt, p + "_rev", st)
        data[rev_key].edge_index = ei.flip(0)   # swap rows: destinations become sources

    # ------------------------------------------------------------------
    # (5) LABELS that aren't edges
    # ------------------------------------------------------------------
    # Correlation-head positives: every rule:R* Correlated pair within this
    # incident. In R-1 every alarm in a graph shares an incident, so the
    # meaningful learning signal comes from CROSS-incident negatives sampled at
    # batch time (§5.3) — hence we store a pair index, not an edge, and let the
    # training loop draw negatives against other incidents in the mini-batch.
    if corr_pairs:
        idx = torch.tensor(sorted(corr_pairs), dtype=torch.long)
        data[ALARM_NODE_TYPE].corr_pos = idx      # [P, 2] — dim-0 = pairs, batch-friendly
    else:
        data[ALARM_NODE_TYPE].corr_pos = torch.empty(0, 2, dtype=torch.long)

    # Secondary aux label (§5.4): 1 where the simulator flagged the alarm as a
    # secondary symptom, else 0. Cheap extra supervision, free from the CSV.
    sec = torch.zeros(n_alarm, dtype=torch.long)
    for i, v in label_secondary:
        sec[i] = v
    data[ALARM_NODE_TYPE].y_secondary = sec

    return data


# ===========================================================================
# Dataset driver — build + cache one HeteroData per incident
# ===========================================================================

def load_inputs(kg_dir: str = DEFAULT_KG_DIR):
    """Load the three KG tables from ``data/kg/`` into pandas DataFrames.

    Returns ``(alarm_csv, topo_csv, manifest)``. Read once per build run and
    reused across all incidents (the CSVs are large; re-reading per incident
    would dominate runtime).
    """
    alarm_csv = pd.read_csv(os.path.join(kg_dir, "alarm_triples.csv"))
    topo_csv = pd.read_csv(os.path.join(kg_dir, "topology_triples.csv"))
    manifest = pd.read_csv(os.path.join(kg_dir, "incident_manifest.csv"))
    return alarm_csv, topo_csv, manifest


def incident_ids(manifest: pd.DataFrame, suppressed: bool | None = None) -> list[int]:
    """All incident ids in the manifest, sorted; optionally filtered by the
    ``suppressed`` flag.

    ``suppressed=None`` returns both the raw and the L1-suppressed views.
    Callers usually pass ``suppressed=False`` to avoid double-counting the same
    fault (each fault is stored once raw, once suppressed — Design/00 §3.7).
    """
    df = manifest
    if suppressed is not None:
        df = df[df["suppressed"] == suppressed]
    return sorted(int(x) for x in df["incident_id"].unique())


def build_dataset(
    incident_ids: Iterable[int],
    fb: FeatureBundle | None = None,
    kg_dir: str = DEFAULT_KG_DIR,
    db_path: str = DEFAULT_DB,
    cache_dir: str | None = None,
    with_rule_edges: bool = False,
    name_dim: int = 32,
    progress=True,
    skip_empty: bool = True,
) -> list[HeteroData]:
    """Build (and optionally cache) one ``HeteroData`` per requested incident id.

    Parameters
    ----------
    incident_ids : the incidents to build.
    fb : pre-built feature bundle (recommended — pass the same one to the model
        later). If ``None``, it is discovered from ``kg_dir``/``db_path``.
    cache_dir : if given, each graph is saved as
        ``incident_<id>_<regime>.pt`` (``<regime>`` = ``pure`` or ``rule``) and
        reloaded on subsequent runs instead of rebuilt. This is the §8/Q5 cache
        — avoids re-parsing the large CSV every epoch.
    with_rule_edges : the §5.5 regime toggle (see ``build_incident_graph``).
    name_dim : alarm-name embedding width (model hyperparameter; sizing only).
    progress : print a one-line progress summary every 25 incidents.
    skip_empty : silently skip incidents with no alarm triples in the CSV
        (rather than raising). The contaminated demo DB has a few such gaps;
        they carry no signal so skipping is the right default.

    Returns a list of ``HeteroData``, one per id that had triples, in input order.
    """
    alarm_csv, topo_csv, manifest = load_inputs(kg_dir)
    if fb is None:
        fb = build_feature_bundle(kg_dir, db_path)
    # Build the shared topology spine ONCE; its edge tensors are copied into
    # every incident graph.
    topo = TopologyIndex(topo_csv, fb)
    topo_edges = topo.edge_tensors()

    graphs: list[HeteroData] = []
    ids = list(incident_ids)
    for k, iid in enumerate(ids):
        # ---- cache: load if present, else build + save ----
        if cache_dir is not None:
            os.makedirs(cache_dir, exist_ok=True)
            tag = "rule" if with_rule_edges else "pure"
            cache_path = os.path.join(cache_dir, f"incident_{iid}_{tag}.pt")
            if os.path.exists(cache_path):
                graphs.append(torch.load(cache_path, weights_only=False))
                continue
        try:
            g = build_incident_graph(
                iid, alarm_csv, manifest, topo_edges, fb, db_path,
                with_rule_edges=with_rule_edges, name_dim=name_dim,
            )
        except ValueError:
            if skip_empty:
                continue   # incident has no alarm triples — nothing to learn
            raise
        if cache_dir is not None:
            torch.save(g, cache_path)
        graphs.append(g)
        if progress and (k % 25 == 0 or k == len(ids) - 1):
            print(f"  built {k + 1}/{len(ids)} (incident {iid}: "
                  f"{g[ALARM_NODE_TYPE].num_nodes} alarms, "
                  f"{len([k for k in g.edge_types])} edge types)")
    return graphs
