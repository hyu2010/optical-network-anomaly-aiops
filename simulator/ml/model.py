"""The GNN model: R-GCN backbone + root-detection head (Design/11 §5.1, §5.2).

ML-2 scope: **backbone + root head only** (no correlation head — that's ML-3).
The goal of this phase is the overfit sanity check: on 50 incidents, the loss
should descend and root accuracy should climb to ~100%, proving the pipeline
wires end-to-end. It is *not* expected to generalise yet.

Architecture
------------
The graphs are built as ``HeteroData`` (for clarity + label storage in ML-1),
but the backbone is a flattened R-GCN: we convert each graph to homogeneous
form (``to_homogeneous``) so a single ``RGCNConv`` handles all (node-type,
predicate, node-type) triples as distinct *relations*. Concretely:

  * every edge type (e.g. ``('alarm','AtNE','ne')``, ``('ne','AtNE_rev','alarm')``)
    becomes one relation id in ``[0, num_relations)``;
  * every node (across all types) gets one row in a big feature matrix, with a
    stable per-type offset so we can slice the alarm rows back out;
  * two ``RGCNConv`` layers (hidden=64, out=64) message-pass over all of them.

Node features
-------------
  * ``alarm`` nodes    : dense vector from features.py (layer/severity one-hot +
                         Δt + is_first) CONCATENATED with the alarm-name
                         embedding (looked up from ``name_idx``). This concat
                         happens here, not in dataset.py, so the embedding is a
                         *learned* parameter of the model.
  * topology nodes     : a learned per-type embedding (``nn.Embedding`` per
                         type) — they have no input features; identity comes
                         purely from being a distinct global id (§4.2/§5.6).
  * ``incident`` node  : its 2-d temporal-flag vector, zero-padded to hidden.

Heads
-----
Only the **root head** in ML-2: an MLP over each alarm's output embedding ->
scalar logit. Per §5.2, inference is ``argmax`` over the incident's alarm logits
(pick exactly one root), matching the rules-baseline protocol for fair
comparison.
"""
from __future__ import annotations

from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import HeteroData, Batch
from torch_geometric.nn import RGCNConv

from .features import FeatureBundle, ALARM_NODE_TYPE, INCIDENT_NODE_TYPE


def relation_vocab(graphs) -> tuple[list[tuple[str, str, str]], int]:
    """The full edge-type vocabulary across a dataset + its size.

    Returns ``(sorted_edge_type_list, num_relations)``. Pass the list as
    ``edge_type_order`` and the size as ``num_relations`` to ``RootAlarmGNN``
    so the convs are sized against the dataset-wide UNION of edge types — not
    every incident contains every edge type, so sizing per-batch would either
    truncate (out-of-bounds relation id) or shift ids across batches.
    """
    rels: set[tuple[str, str, str]] = set()
    for g in graphs:
        rels.update(g.edge_types)
    order = sorted(rels)
    return order, len(order)


class RootAlarmGNN(nn.Module):
    """R-GCN backbone + alarm-name embedding + root-detection head.

    Parameters
    ----------
    fb : the feature bundle (gives vocab sizes: #alarm-names, #layers, ...
         #topology nodes per type). The same bundle the dataset was built with.
    hidden : R-GCN hidden + output dim (Design/11 §5.1 default 64).
    name_dim : alarm-name embedding width (§4.1 default 32).
    dropout : dropout applied between conv layers (§5.1 default 0.2).
    """

    def __init__(self, fb: FeatureBundle, hidden: int = 64, name_dim: int = 32,
                 dropout: float = 0.2, num_relations: int | None = None,
                 edge_type_order: list[tuple[str, str, str]] | None = None) -> None:
        super().__init__()
        self.fb = fb
        self.hidden = hidden
        self.name_dim = name_dim

        # The relation vocabulary: the FULL set of (src_type, pred, dst_type)
        # edge types the model must handle. Not every incident contains every
        # edge type (e.g. some faults produce no AtSpan edges), so we size the
        # R-GCN against the UNION over the whole dataset (see
        # ``relation_vocab``), not per batch. The canonical id of a relation is
        # its index in ``edge_type_order``.
        self.edge_type_order: list[tuple[str, str, str]] | None = list(edge_type_order) \
            if edge_type_order is not None else None
        if num_relations is not None:
            self.num_relations = num_relations
            self.conv1 = RGCNConv(hidden, hidden, num_relations)
            self.conv2 = RGCNConv(hidden, hidden, num_relations)
        else:
            # Lazy init: size on first forward once we see a real edge set.
            self.num_relations = None
            self.conv1 = None
            self.conv2 = None

        # ---- input encoders (turn each node type into a `hidden`-dim vector) ----

        # alarm: dense features (layer/sev/dt/is_first) + learned name embedding.
        dense_dim = len(fb.layers) + len(fb.severities) + 1 + 1   # see features.dim_alarm_feature minus name_dim
        self.name_emb = nn.Embedding(len(fb.alarm_names), name_dim)
        self.alarm_in = nn.Linear(dense_dim + name_dim, hidden)

        # topology nodes: one nn.Embedding table per type (§4.2). Each table is
        # indexed by the GLOBAL id stored on the node block (so NE "A" / span
        # OMS3_BC share one vector across all incidents).
        self.topo_types = sorted(fb.topo.by_type.keys())
        self.topo_emb = nn.ModuleDict({
            t: nn.Embedding(len(fb.topo.by_type[t]), hidden)
            for t in self.topo_types
        })

        # incident super-node: 2-d temporal flags -> hidden (zero-padded linear).
        self.incident_in = nn.Linear(2, hidden)

        # ---- backbone: 2 R-GCN layers over the homogeneous graph ----
        # NOTE: num_relations / conv1 / conv2 were already set in the relation-
        # vocabulary block at the top of __init__ (either from the constructor
        # args or lazily on first forward via _init_relation_space). An earlier
        # version re-declared them as None here, silently wiping the constructor
        # values — that made load_run() rebuild convs with random weights.
        self.act = nn.GELU()
        self.drop = nn.Dropout(dropout)

        # ---- root head (§5.2) ----
        self.root_head = nn.Sequential(
            nn.Linear(hidden, 32), nn.GELU(),
            nn.Linear(32, 1),
        )

        # ---- correlation head (§5.3, ML-3) ----
        # Per-alarm-pair classifier: "do these two alarms belong together?" It
        # reads two alarm embeddings concatenated -> one logit. Reuses the same
        # backbone embeddings as the root head (the model answers BOTH questions
        # off one R-GCN).
        self.corr_head = nn.Sequential(
            nn.Linear(2 * hidden, 64), nn.GELU(),
            nn.Linear(64, 1),
        )

    # ------------------------------------------------------------------
    # Relation vocabulary + conv init
    # ------------------------------------------------------------------
    def _init_relation_space(self, batch: Batch) -> None:
        """Lazily size the R-GCN convs against the FULL edge-type vocabulary.

        Only used when the caller did NOT pass ``num_relations`` at construction.
        We adopt the first batch's ``edge_types`` as the canonical vocabulary;
        ``_remap_relations`` will extend it (and we must resize the convs) if a
        later batch reveals a relation the first batch lacked. Prefer passing
        ``num_relations`` from the dataset-wide union (see ``relation_vocab``)
        to avoid the resize dance entirely.
        """
        if self.num_relations is not None:
            return
        self.edge_type_order = list(batch.edge_types)
        self.num_relations = len(self.edge_type_order)
        dev = next(self.parameters()).device
        self.conv1 = RGCNConv(self.hidden, self.hidden, self.num_relations).to(dev)
        self.conv2 = RGCNConv(self.hidden, self.hidden, self.num_relations).to(dev)

    def _remap_relations(self, batch: Batch, edge_type: torch.Tensor) -> torch.Tensor:
        """Map a batch's per-batch relation ids into the model's fixed id space.

        ``to_homogeneous`` numbers relations by the sorted ``batch.edge_types``
        order, which is stable but batch-specific (a batch missing an edge type
        has fewer ids). We translate each id into the model's canonical id using
        ``batch.edge_types`` -> ``self.edge_type_order``.

        When the model was constructed with the dataset-wide union
        (``edge_type_order`` from ``relation_vocab``), every batch relation is a
        subset and maps cleanly — no resizing. (Lazy mode below handles a
        genuinely new relation by extending the vocab.)
        """
        batch_names = batch.edge_types
        canonical = {name: i for i, name in enumerate(self.edge_type_order)}
        lut = torch.zeros(len(batch_names), dtype=torch.long, device=edge_type.device)
        for i, name in enumerate(batch_names):
            if name not in canonical:
                # New relation appeared after construction (lazy mode only).
                canonical[name] = len(self.edge_type_order)
                self.edge_type_order.append(name)
            lut[i] = canonical[name]
        if lut.max().item() >= self.num_relations:
            # Lazy mode: a later batch revealed a relation beyond the conv size.
            self.num_relations = int(lut.max().item()) + 1
            dev = next(self.parameters()).device
            self.conv1 = RGCNConv(self.hidden, self.hidden, self.num_relations).to(dev)
            self.conv2 = RGCNConv(self.hidden, self.hidden, self.num_relations).to(dev)
        return lut[edge_type]

    def _build_node_features(self, batch: Batch) -> tuple[torch.Tensor, torch.Tensor]:
        """Concatenate every node type's features into one [N_total, hidden] matrix.

        Node ordering follows ``batch.node_types``; each type occupies a
        contiguous block. We also return the per-type block offsets so callers
        can slice a specific type back out.
        """
        xs: list[torch.Tensor] = []
        device = batch[ALARM_NODE_TYPE].x.device
        for t in batch.node_types:
            ntype = batch[t]
            n = ntype.num_nodes
            if t == ALARM_NODE_TYPE:
                dense = ntype.x                                          # [n, dense_dim]
                names = self.name_emb(ntype.name_idx)                    # [n, name_dim]
                x = self.alarm_in(torch.cat([dense, names], dim=1))      # -> [n, hidden]
            elif t == INCIDENT_NODE_TYPE:
                x = self.incident_in(ntype.x)                           # [1, 2] -> [1, hidden]
            elif t in self.topo_emb:
                gid = ntype.global_id.to(device)
                x = self.topo_emb[t](gid)                               # [n, hidden]
            else:
                # Unknown node type (shouldn't happen) — zero embed so the
                # forward doesn't crash; will not receive meaningful gradients.
                x = torch.zeros(n, self.hidden, device=device)
            xs.append(x)
        return torch.cat(xs, dim=0)

    def _alarm_global_rows(self, batch: Batch) -> torch.Tensor:
        """The indices (into the homogeneous node matrix) of every ALARM node.

        Node blocks are laid out per ``batch.node_types`` in order; each block
        occupies a contiguous range. We sum the sizes of the blocks that come
        *before* ``alarm`` and then emit ``[offset .. offset+n)``.
        """
        offset = 0
        for t in batch.node_types:
            if t == ALARM_NODE_TYPE:
                return torch.arange(offset, offset + batch[t].num_nodes)
            offset += batch[t].num_nodes
        return torch.empty(0, dtype=torch.long)

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------
    def forward(self, batch: Batch) -> dict[str, torch.Tensor]:
        """Run the backbone + heads over a batched heterogeneous graph.

        Returns a dict with:
          * ``z_alarm``     : [N_alarm, hidden] the per-alarm embeddings
                              (used by both the root head and the correlation
                              head, and returned for the corr head / explanation);
          * ``root_logits`` : [N_alarm] per-alarm root logits (pre-sigmoid);
          * ``alarm_batch`` : [N_alarm] which incident each alarm belongs to
                              (for grouping argmax-over-incident at eval).
        """
        device = batch[ALARM_NODE_TYPE].x.device

        # Homogenise edges: to_homogeneous() merges all (src_type,pred,dst_type)
        # edge types into one edge_index + an edge_type tensor of relation ids
        # in [0, R). It numbers relations by the sorted ``batch.edge_types``
        # order (stable across batches, but a batch may contain a SUBSET of the
        # full edge vocabulary — see _remap_relations).
        homo = batch.to_homogeneous()
        edge_index, edge_type = homo.edge_index, homo.edge_type

        # Size the convs against the full edge-type vocabulary (lazy, once).
        self._init_relation_space(batch)
        # Translate this batch's relation ids into the canonical id space so a
        # missing edge type in one batch doesn't shift every other relation's id.
        edge_type = self._remap_relations(batch, edge_type)

        # Build the unified node feature matrix (per-type encoders above).
        x = self._build_node_features(batch)

        # Two R-GCN message-passing layers (§5.1). Each layer updates every
        # node by aggregating per-relation messages from its neighbours.
        x = self.act(x)
        h = self.drop(self.act(self.conv1(x, edge_index, edge_type)))
        h = self.conv2(h, edge_index, edge_type)            # [N_total, hidden]

        # Slice the alarm rows back out and apply the root head.
        alarm_rows = self._alarm_global_rows(batch).to(device)
        z_alarm = h[alarm_rows]                             # [N_alarm, hidden]
        root_logits = self.root_head(z_alarm).squeeze(-1)   # [N_alarm]

        return {
            "z_alarm": z_alarm,
            "root_logits": root_logits,
            "alarm_batch": batch[ALARM_NODE_TYPE].batch.to(device),
        }

    # ------------------------------------------------------------------
    # Correlation head — pair scoring (§5.3, ML-3)
    # ------------------------------------------------------------------
    def score_pairs(self, z_alarm: torch.Tensor, pairs: torch.Tensor) -> torch.Tensor:
        """Score a set of alarm pairs -> per-pair logit.

        ``pairs`` is a [P, 2] long tensor of alarm-row indices into ``z_alarm``
        (i.e. into the N_alarm axis). Returns [P] logits.
        """
        i, j = pairs[:, 0], pairs[:, 1]
        z_pair = torch.cat([z_alarm[i], z_alarm[j]], dim=1)   # [P, 2*hidden]
        return self.corr_head(z_pair).squeeze(-1)              # [P]

    # ------------------------------------------------------------------
    # Loss + accuracy helpers
    # ------------------------------------------------------------------
    def root_loss(self, fwd: dict, y_root: torch.Tensor) -> torch.Tensor:
        """BCE-with-logits on per-alarm root logits, reweighted for the ~50:1
        class imbalance (§5.2: ``pos_weight = neg/pos``).

        ``y_root`` is the per-alarm 0/1 label (exactly one 1 per incident).
        """
        logit = fwd["root_logits"]
        pos = y_root.sum().clamp(min=1).float()
        neg = (y_root.numel() - pos).clamp(min=1).float()
        pos_weight = torch.tensor((neg / pos).item(), device=logit.device)
        return F.binary_cross_entropy_with_logits(logit, y_root.float(), pos_weight=pos_weight)


@torch.no_grad()
def root_accuracy(fwd: dict, y_root: torch.Tensor) -> float:
    """Per-incident argmax accuracy against a (possibly multi-root) root set.

    Protocol (§5.2/§7.1): pick exactly one root per incident — the argmax-logit
    alarm — then count it correct if it lands in the labelled root set (the KG
    picker may name 2 roots for compound faults). Returns the fraction of
    incidents in the batch predicted correctly.
    """
    logit = fwd["root_logits"]
    batch_idx = fwd["alarm_batch"]
    correct = 0
    total = 0
    for b in batch_idx.unique().tolist():
        mask = batch_idx == b
        if not mask.any():
            continue
        local_rows = mask.nonzero(as_tuple=True)[0]
        pred_row = int(local_rows[int(logit[mask].argmax())])
        root_set = set(local_rows[y_root[mask] == 1].tolist())
        if not root_set:
            continue   # unlabelled incident — don't count it either way
        correct += int(pred_row in root_set)
        total += 1
    return correct / total if total else 0.0


def corr_loss_from_pairs(logits: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
    """BCE-with-logits over a batch of pair scores + 0/1 labels (§5.3).

    ``labels`` is 1 if the two alarms belong to the same incident (positive),
    0 if they were sampled cross-incident (negative). No pos_weight: the
    negative sampler already balances the ratio (~3:1 neg:pos).
    """
    return F.binary_cross_entropy_with_logits(logits, labels.float())


@torch.no_grad()
def pair_correlation_f1(logits: torch.Tensor, labels: torch.Tensor,
                        threshold: float = 0.5) -> tuple[float, float, float]:
    """Precision / Recall / F1 over pair scores at ``threshold`` (§7.1).

    Used on held-out data: positives = same-incident pairs, negatives =
    cross-incident pairs (sampled the same way as training). Returns
    ``(precision, recall, f1)``.
    """
    pred = (torch.sigmoid(logits) >= threshold).long()
    tp = int(((pred == 1) & (labels == 1)).sum())
    fp = int(((pred == 1) & (labels == 0)).sum())
    fn = int(((pred == 0) & (labels == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1
