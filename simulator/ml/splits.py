"""Train/val/test splitting + cross-incident negative pair sampling.

Design/11 §6.1 (splits) + §5.3 (correlation-head negatives).

Splitting rule (§6.1): stratify by ``fault_type`` so every split sees every
fault class, AND keep whole ``(fault_type, fault_target)`` groups in the *same*
split. This matters here because the current dataset has many near-duplicate
cascades — two incidents with the same (fault_type, fault_target) are the same
fault on the same span and are highly correlated. Putting one in train and one
in test would let the model memorise the cascade and "predict" its twin — i.e.
leakage. Grouping defeats that.

Cross-incident negatives (§5.3): within one incident every alarm pair is a
positive ("same group"), so the correlation head only learns from NEGATIVES —
pairs sampled from two DIFFERENT incidents in the same mini-batch, at ~3:1
neg:pos. The sampler below draws such pairs given a batch's per-alarm incident
membership (PyG's ``batch`` vector).
"""
from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd
import torch
from torch_geometric.data import Batch  # noqa: F401  (used in type hint)

from .dataset import DEFAULT_KG_DIR, incident_ids, load_inputs


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

@dataclass
class Split:
    """An incident-id split (train / val / test)."""
    train: list[int]
    val: list[int]
    test: list[int]

    def to_json(self) -> dict:
        return {"train": self.train, "val": self.val, "test": self.test}

    @classmethod
    def from_json(cls, d: dict) -> "Split":
        return cls(train=list(d["train"]), val=list(d["val"]), test=list(d["test"]))


def make_split(
    manifest: pd.DataFrame,
    ratios: tuple[float, float, float] = (0.70, 0.15, 0.15),
    seed: int = 0,
    suppressed: bool = False,
) -> Split:
    """Stratified 70/15/15 split, grouping by ``(fault_type, fault_target)``.

    Parameters
    ----------
    manifest : the incident_manifest table.
    ratios : (train, val, test) fractions (need not sum exactly to 1).
    seed : RNG seed (§6.4 reproducibility).
    suppressed : split the raw (False) or suppressed (True) view. The two views
        of one fault belong in the SAME split; we split the raw view and the
        caller maps suppressed ids into the same bucket via
        ``map_suppressed_split`` (or just trains on raw — the default).

    Algorithm: bucket incident ids by ``(fault_type, fault_target)`` group;
    sort groups by fault_type for stratification; walk the groups in a seeded
    random order and assign each WHOLE group to the split currently furthest
    below its target ratio. This keeps near-duplicate cascades together while
    approximating the per-fault-type ratio.
    """
    df = manifest[manifest["suppressed"] == suppressed].copy()
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for _, r in df.iterrows():
        groups[(str(r["fault_type"]), str(r["fault_target"]))].append(int(r["incident_id"]))

    # sort fault types so the walk is stratified and deterministic
    fault_types = sorted({k[0] for k in groups})
    rng = random.Random(seed)

    targets = np.array(ratios) / sum(ratios)
    counts = np.zeros(3, dtype=float)
    buckets: list[list[int]] = [[], [], []]   # train, val, test

    # iterate fault_type by fault_type, shuffling that type's groups
    for ft in fault_types:
        ft_groups = [g for g in groups if g[0] == ft]
        rng.shuffle(ft_groups)
        for gk in ft_groups:
            ids = sorted(groups[gk])
            # assign this whole group to the split furthest below its target
            frac = counts / max(counts.sum(), 1.0)
            deficit = targets - frac
            dest = int(np.argmax(deficit))
            buckets[dest].extend(ids)
            counts[dest] += len(ids)

    return Split(train=sorted(buckets[0]), val=sorted(buckets[1]), test=sorted(buckets[2]))


def save_split(split: Split, path: str) -> None:
    """Write a split to JSON (``data/ml/splits.json``)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(split.to_json(), f, indent=1)


def load_split(path: str) -> Split:
    with open(path, encoding="utf-8") as f:
        return Split.from_json(json.load(f))


# ---------------------------------------------------------------------------
# Cross-incident negative sampler (§5.3)
# ---------------------------------------------------------------------------

def sample_pairs_for_batch(
    batch_alarm_incident: torch.Tensor,
    pos_pairs: list[torch.Tensor],
    neg_ratio: float = 3.0,
    seed: int | None = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Build a (pairs, labels) tensor for the correlation head on one mini-batch.

    Parameters
    ----------
    batch_alarm_incident : [N] the incident id of every alarm in the batch
        (PyG's ``batch['alarm'].incident_id``, OR a per-alarm batch-index
        vector — see note). Used to tell which alarms are "same incident".
    pos_pairs : list of [P_k, 2] positive pair tensors, one per incident in the
        batch (the ``corr_pos`` we cached on each graph in ML-1). Already indexed
        into the GLOBAL alarm axis of the batch (the caller offsets them).
    neg_ratio : negatives per positive (§5.3 recommends 3:1).

    Returns
    -------
    (pairs, labels) : pairs is [P_total, 2] long (alarm rows in the batch),
        labels is [P_total] (1 positive, 0 negative).

    Note: "same incident" for negatives is decided by ``batch_alarm_incident``
    being EQUAL across two sampled alarms. The caller must pass a vector that
    is identical for two alarms iff they are in the same incident (the per-graph
    ``incident_id`` works; so does the PyG batch index since each incident is
    its own disconnected graph).
    """
    n = batch_alarm_incident.shape[0]
    rng = random.Random(seed) if seed is not None else random

    # ---- positives (offset already applied by caller) ----
    if pos_pairs:
        pos = torch.cat(pos_pairs, dim=0)
    else:
        pos = torch.empty(0, 2, dtype=torch.long)
    n_pos = pos.shape[0]
    n_neg = int(round(neg_ratio * n_pos))

    # ---- negatives: random cross-incident pairs ----
    inc = batch_alarm_incident.tolist()
    negs: list[tuple[int, int]] = []
    # cap attempts to avoid an infinite loop when most alarms share an incident
    attempts = 0
    max_attempts = max(20 * n_neg, 100)
    seen = set()
    while len(negs) < n_neg and attempts < max_attempts:
        attempts += 1
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j:
            continue
        a, b = (i, j) if i < j else (j, i)
        if (a, b) in seen:
            continue
        if inc[a] == inc[b]:
            continue                    # same incident -> not a valid negative
        seen.add((a, b))
        negs.append((a, b))

    if negs:
        neg = torch.tensor(negs, dtype=torch.long)
        pairs = torch.cat([pos, neg], dim=0)
        labels = torch.cat([torch.ones(n_pos), torch.zeros(len(negs))])
    else:
        pairs = pos
        labels = torch.ones(n_pos) if n_pos else torch.empty(0)

    return pairs, labels


def offset_pos_pairs_for_batch(batch: "Batch") -> torch.Tensor:
    """Return this mini-batch's positive correlation pairs, offset into the
    batch's GLOBAL alarm axis.

    PyG concatenates each graph's ``corr_pos`` along dim 0 but does NOT add the
    per-graph alarm offset to the pair indices — so graph k's pairs still
    reference rows local to graph k. We fix that here using the cumulative
    alarm slices PyG stores in ``batch._slice_dict['alarm']['x']``.

    Returns a single [P_total, 2] long tensor (empty if no positives).
    """
    a = batch["alarm"]
    if a.corr_pos is None or a.corr_pos.shape[0] == 0:
        return torch.empty(0, 2, dtype=torch.long)
    cum = batch._slice_dict["alarm"]["x"]      # [0, n0, n0+n1, ...]
    # corr_pos was concatenated in graph order; split & offset per graph.
    cum_pairs = batch._slice_dict["alarm"]["corr_pos"]
    parts = []
    for k in range(len(cum) - 1):
        p0, p1 = int(cum_pairs[k]), int(cum_pairs[k + 1])
        if p1 == p0:
            continue
        cp = a.corr_pos[p0:p1]
        parts.append(cp + int(cum[k]))
    return torch.cat(parts, dim=0) if parts else torch.empty(0, 2, dtype=torch.long)
