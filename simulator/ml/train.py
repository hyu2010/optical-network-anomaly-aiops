"""Training + evaluation for the full two-head GNN (Design/11 §5.3, §5.4, §6.2).

ML-3 scope: a *generalizing* trainer with real train/val/test splits, the
correlation head + cross-incident negatives, a multi-task loss, a real schedule,
and early stopping on validation root accuracy. This replaces the overfit-only
``train_root_head`` from ML-2 for any real run.

The earlier ML-2 ``train_root_head`` is kept (the overfit sanity check still
uses it); this module adds the production-style training path.

What's here (the §6.2 subset relevant to a two-head generalization run):
  * stratified 70/15/15 split (splits.py), grouped by (fault_type, fault_target);
  * multi-task loss ``L = L_root + lambda_corr * L_corr``;
  * linear warmup -> cosine decay LR schedule;
  * gradient clipping (max-norm 1.0);
  * early stopping on val ``root_accuracy`` (patience);
  * per-epoch metrics: train/val loss + root accuracy + pair-correlation F1;
  * a final test-set evaluation.

Deliberately deferred to ML-4+: hyperparameter search, the aux fault-class /
secondary heads (the loss is wired for them but they're off by default), and
beating the 99.1% rules baseline.
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import torch
from torch_geometric.data import Batch

from .features import ALARM_NODE_TYPE, FeatureBundle
from .model import (
    RootAlarmGNN,
    corr_loss_from_pairs,
    pair_correlation_f1,
    relation_vocab,
    root_accuracy,
)
from .splits import offset_pos_pairs_for_batch, sample_pairs_for_batch


# ---------------------------------------------------------------------------
# Reproducibility (§6.4)
# ---------------------------------------------------------------------------

def set_seed(seed: int) -> None:
    """Seed Python/numpy/torch for reproducible runs (§6.4)."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


# ---------------------------------------------------------------------------
# Overfit loop (ML-2 — kept for the sanity check)
# ---------------------------------------------------------------------------

@dataclass
class TrainConfig:
    """Hyperparameters for the ML-2 overfit run."""
    epochs: int = 200
    batch_size: int = 8
    lr: float = 5e-4
    weight_decay: float = 1e-4
    grad_clip: float = 1.0
    hidden: int = 64
    name_dim: int = 32
    dropout: float = 0.2
    seed: int = 0
    log_every: int = 10
    device: str = "cpu"


@dataclass
class TrainHistory:
    epochs: list[int] = field(default_factory=list)
    loss: list[float] = field(default_factory=list)
    acc: list[float] = field(default_factory=list)


def _batches(graphs, batch_size: int, shuffle: bool = True, seed: int | None = None):
    """Yield ``(Batch, [graph refs])`` over the graph list. We also return the
    list of source graphs so the correlation-head sampler can offset per-graph
    pair indices (PyG doesn't auto-offset corr_pos)."""
    idx = list(range(len(graphs)))
    if shuffle:
        rng = random.Random(seed)
        rng.shuffle(idx)
    for i in range(0, len(idx), batch_size):
        chunk = [graphs[j] for j in idx[i:i + batch_size]]
        yield Batch.from_data_list(chunk), chunk


def train_root_head(
    graphs: list,
    fb: FeatureBundle,
    cfg: TrainConfig | None = None,
    progress: Callable[[int, float, float], None] | None = None,
) -> tuple[RootAlarmGNN, TrainHistory]:
    """Train the root head to overfit on ``graphs`` (ML-2 sanity check)."""
    cfg = cfg or TrainConfig()
    set_seed(cfg.seed)
    device = torch.device(cfg.device if cfg.device != "auto" else
                          ("cuda" if torch.cuda.is_available() else "cpu"))

    edge_type_order, num_relations = relation_vocab(graphs)
    model = RootAlarmGNN(fb, hidden=cfg.hidden, name_dim=cfg.name_dim,
                         dropout=cfg.dropout, num_relations=num_relations,
                         edge_type_order=edge_type_order).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    history = TrainHistory()

    for epoch in range(1, cfg.epochs + 1):
        model.train()
        epoch_loss = 0.0
        epoch_acc = 0.0
        n_batches = 0
        for batch, _ in _batches(graphs, cfg.batch_size, shuffle=True, seed=cfg.seed + epoch):
            batch = batch.to(device)
            y_root = batch[ALARM_NODE_TYPE].y_root.to(device)
            fwd = model(batch)
            loss = model.root_loss(fwd, y_root)
            opt.zero_grad()
            loss.backward()
            if cfg.grad_clip:
                torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
            opt.step()
            epoch_loss += float(loss.detach())
            epoch_acc += root_accuracy(fwd, y_root)
            n_batches += 1
        epoch_loss /= max(1, n_batches)
        epoch_acc /= max(1, n_batches)
        history.epochs.append(epoch)
        history.loss.append(epoch_loss)
        history.acc.append(epoch_acc)
        if progress:
            progress(epoch, epoch_loss, epoch_acc)
        elif epoch % cfg.log_every == 0 or epoch == 1 or epoch == cfg.epochs:
            print(f"  epoch {epoch:4d}  loss={epoch_loss:.4f}  root_acc={epoch_acc:.3f}", file=sys.stderr)
    return model, history


# ===========================================================================
# ML-3: generalizing two-head trainer
# ===========================================================================

@dataclass
class MTLConfig:
    """Hyperparameters for the ML-3 generalization run (two heads)."""
    epochs: int = 60
    batch_size: int = 16
    lr: float = 5e-4
    weight_decay: float = 1e-4
    grad_clip: float = 1.0
    hidden: int = 64
    name_dim: int = 32
    dropout: float = 0.2
    seed: int = 0
    warmup_steps: int = 200
    lr_min: float = 5e-6
    lambda_corr: float = 0.5     # §5.4 multi-task weight on L_corr
    neg_ratio: float = 3.0       # §5.3 cross-incident neg:pos
    patience: int = 10           # early stopping on val root accuracy
    eval_neg_per_incident: int = 30   # negatives drawn per incident at eval
    log_every: int = 5
    device: str = "cpu"


@dataclass
class MTLHistory:
    train_loss: list[float] = field(default_factory=list)
    train_root_acc: list[float] = field(default_factory=list)
    val_root_acc: list[float] = field(default_factory=list)
    val_corr_f1: list[float] = field(default_factory=list)
    best_val_root_acc: float = -1.0
    best_epoch: int = -1


def _lr_schedule(step: int, total_steps: int, base_lr: float, min_lr: float,
                 warmup: int) -> float:
    """Linear warmup -> cosine decay to ``min_lr`` (§6.2)."""
    if step < warmup:
        return base_lr * (step + 1) / max(1, warmup)
    progress = (step - warmup) / max(1, total_steps - warmup)
    progress = min(max(progress, 0.0), 1.0)
    return min_lr + 0.5 * (base_lr - min_lr) * (1 + math.cos(math.pi * progress))


def _eval_pairs(model, batch, neg_ratio: float, seed: int) -> tuple[float, float]:
    """Evaluate pair-correlation P/R/F1 on a batch (held-out).

    Draws the same kind of cross-incident negatives as training, then scores
    all pairs and reports F1. ``batch`` is on the model's device.
    """
    a = batch[ALARM_NODE_TYPE]
    inc = a.batch                                  # per-alarm graph index (== incident id in-batch)
    pos = offset_pos_pairs_for_batch(batch).to(a.x.device)
    if pos.shape[0] == 0:
        return 0.0, 0.0
    pairs, labels = sample_pairs_for_batch(inc, [pos], neg_ratio=neg_ratio, seed=seed)
    pairs = pairs.to(a.x.device)
    labels = labels.to(a.x.device)
    with torch.no_grad():
        logits = model.score_pairs(model(batch)["z_alarm"], pairs)
    p, r, f1 = pair_correlation_f1(logits, labels)
    return p, f1  # caller uses precision + f1 (recall implied by them)


def train_mtl(
    train_graphs: list,
    val_graphs: list,
    fb: FeatureBundle,
    cfg: MTLConfig | None = None,
    progress: Callable[[int, dict], None] | None = None,
) -> tuple[RootAlarmGNN, MTLHistory]:
    """Train the two-head GNN with multi-task loss + schedule + early stopping.

    Returns ``(model, history)``. The model is the one at the best val root
    accuracy (early stopping restored in-memory via best seen).
    """
    cfg = cfg or MTLConfig()
    set_seed(cfg.seed)
    device = torch.device(cfg.device if cfg.device != "auto" else
                          ("cuda" if torch.cuda.is_available() else "cpu"))

    edge_type_order, num_relations = relation_vocab(train_graphs + val_graphs)
    model = RootAlarmGNN(fb, hidden=cfg.hidden, name_dim=cfg.name_dim,
                         dropout=cfg.dropout, num_relations=num_relations,
                         edge_type_order=edge_type_order).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)

    steps_per_epoch = math.ceil(len(train_graphs) / cfg.batch_size)
    total_steps = steps_per_epoch * cfg.epochs
    global_step = 0
    history = MTLHistory()
    best_state = None

    for epoch in range(1, cfg.epochs + 1):
        model.train()
        t_loss = t_acc = t_corr = n = 0
        for batch, _ in _batches(train_graphs, cfg.batch_size, shuffle=True,
                                 seed=cfg.seed + epoch):
            # LR schedule (§6.2)
            lr = _lr_schedule(global_step, total_steps, cfg.lr, cfg.lr_min, cfg.warmup_steps)
            for g in opt.param_groups:
                g["lr"] = lr
            global_step += 1

            batch = batch.to(device)
            a = batch[ALARM_NODE_TYPE]
            y_root = a.y_root.to(device)

            fwd = model(batch)
            loss_root = model.root_loss(fwd, y_root)

            # correlation head: positives (offset) + cross-incident negatives
            inc = a.batch
            pos = offset_pos_pairs_for_batch(batch).to(device)
            pairs, labels = sample_pairs_for_batch(inc, [pos], neg_ratio=cfg.neg_ratio,
                                                   seed=cfg.seed + global_step)
            pairs = pairs.to(device)
            labels = labels.to(device)
            pair_logits = model.score_pairs(fwd["z_alarm"], pairs)
            loss_corr = corr_loss_from_pairs(pair_logits, labels)

            loss = loss_root + cfg.lambda_corr * loss_corr

            opt.zero_grad()
            loss.backward()
            if cfg.grad_clip:
                torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
            opt.step()

            t_loss += float(loss.detach())
            t_acc += root_accuracy(fwd, y_root)
            with torch.no_grad():
                _, _, f1 = pair_correlation_f1(pair_logits, labels)
            t_corr += f1
            n += 1

        t_loss /= max(1, n); t_acc /= max(1, n); t_corr /= max(1, n)

        # ---- validation ----
        model.eval()
        v_acc = v_f1 = vn = 0
        with torch.no_grad():
            for batch, _ in _batches(val_graphs, cfg.batch_size, shuffle=False):
                batch = batch.to(device)
                a = batch[ALARM_NODE_TYPE]
                v_acc += root_accuracy(model(batch), a.y_root.to(device))
                _, f1 = _eval_pairs(model, batch, cfg.neg_ratio, cfg.seed + epoch)
                v_f1 += f1
                vn += 1
        v_acc /= max(1, vn); v_f1 /= max(1, vn)

        history.train_loss.append(t_loss)
        history.train_root_acc.append(t_acc)
        history.val_root_acc.append(v_acc)
        history.val_corr_f1.append(v_f1)

        improved = v_acc > history.best_val_root_acc
        if improved:
            history.best_val_root_acc = v_acc
            history.best_epoch = epoch
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}

        if progress:
            progress(epoch, {"t_loss": t_loss, "t_acc": t_acc, "t_corr": t_corr,
                             "v_acc": v_acc, "v_f1": v_f1, "lr": lr})
        elif epoch % cfg.log_every == 0 or epoch == 1 or improved:
            print(f"  epoch {epoch:3d}  t_loss={t_loss:.4f} t_acc={t_acc:.3f} "
                  f"v_acc={v_acc:.3f} v_f1={v_f1:.3f}  lr={lr:.2e}"
                  + ("  *" if improved else ""), file=sys.stderr)

        # early stopping (patience on val root accuracy)
        if epoch - history.best_epoch >= cfg.patience:
            print(f"  early stop @ epoch {epoch} (best v_acc={history.best_val_root_acc:.3f} "
                  f"@ epoch {history.best_epoch})", file=sys.stderr)
            break

    if best_state is not None:
        model.load_state_dict(best_state)   # restore the best checkpoint
    return model, history


# ---------------------------------------------------------------------------
# Held-out evaluation
# ---------------------------------------------------------------------------

@torch.no_grad()
def evaluate(model: RootAlarmGNN, graphs: list, fb: FeatureBundle,
             cfg: MTLConfig | None = None) -> dict:
    """Evaluate root accuracy + pair-correlation F1 on a held-out set.

    Returns ``{"root_accuracy", "corr_precision", "corr_f1"}``.
    """
    cfg = cfg or MTLConfig()
    device = next(model.parameters()).device
    model.eval()
    acc = f1_sum = p_sum = n = 0
    for batch, _ in _batches(graphs, cfg.batch_size, shuffle=False):
        batch = batch.to(device)
        a = batch[ALARM_NODE_TYPE]
        fwd = model(batch)
        acc += root_accuracy(fwd, a.y_root.to(device))
        p, f1 = _eval_pairs(model, batch, cfg.neg_ratio, cfg.seed)
        p_sum += p; f1_sum += f1
        n += 1
    n = max(1, n)
    return {"root_accuracy": acc / n, "corr_precision": p_sum / n, "corr_f1": f1_sum / n}


# ---------------------------------------------------------------------------
# Run checkpointing (Design/11 §8) — persist weights + config + metrics
# ---------------------------------------------------------------------------

def save_run(
    model: RootAlarmGNN,
    cfg: MTLConfig,
    metrics: dict,
    run_dir: str,
    extra: dict | None = None,
) -> str:
    """Write a §8-style run directory: best.pt + config.json + metrics.json.

    The checkpoint bundles everything needed to rebuild the model faithfully
    at load time (not just the weights):

      * ``best.pt``       — state_dict PLUS ``edge_type_order`` (the relation
                            vocabulary the R-GCN was sized against) and the
                            feature-bundle JSON (vocabularies for names/layers/
                            severities/fault types/topology). Without these the
                            model cannot be reconstructed: per-relation weight
                            matrices are indexed by relation id, and the input
                            encoders are sized by the vocabularies.
      * ``config.json``   — the hyperparameters + anything in ``extra``
                            (e.g. split path, label provenance, ablation flags).
      * ``metrics.json``  — the final metrics dict (train/val/test acc, F1 …).

    Returns the run directory path.
    """
    import json
    import os

    os.makedirs(run_dir, exist_ok=True)

    checkpoint = {
        "model_state": model.state_dict(),
        "edge_type_order": model.edge_type_order,
        "num_relations": model.num_relations,
        "hidden": model.hidden,
        "name_dim": model.name_dim,
        "feature_bundle": model.fb.to_json() if hasattr(model.fb, "to_json") else None,
    }
    torch.save(checkpoint, os.path.join(run_dir, "best.pt"))

    config = {
        "epochs": cfg.epochs, "batch_size": cfg.batch_size, "lr": cfg.lr,
        "weight_decay": cfg.weight_decay, "grad_clip": cfg.grad_clip,
        "hidden": cfg.hidden, "name_dim": cfg.name_dim, "dropout": cfg.dropout,
        "seed": cfg.seed, "warmup_steps": cfg.warmup_steps, "lr_min": cfg.lr_min,
        "lambda_corr": cfg.lambda_corr, "neg_ratio": cfg.neg_ratio,
        "patience": cfg.patience, "device": cfg.device,
    }
    if extra:
        config.update(extra)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=1)

    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=1)

    return run_dir


def load_run(run_dir: str, device: str = "cpu") -> tuple[RootAlarmGNN, dict]:
    """Rebuild a trained model from a §8 run directory.

    Restores the feature bundle, relation vocabulary and weights saved by
    :func:`save_run`, and returns ``(model_in_eval_mode, config_dict)``.
    Use this for ``predict`` / ``explain`` (ML-5/ML-6).
    """
    import json
    import os

    from .features import FeatureBundle

    checkpoint = torch.load(os.path.join(run_dir, "best.pt"),
                            map_location=device, weights_only=False)
    with open(os.path.join(run_dir, "config.json"), encoding="utf-8") as f:
        config = json.load(f)

    fb = FeatureBundle.from_json(checkpoint["feature_bundle"])
    model = RootAlarmGNN(
        fb,
        hidden=checkpoint["hidden"],
        name_dim=checkpoint["name_dim"],
        dropout=config.get("dropout", 0.2),
        num_relations=checkpoint["num_relations"],
        edge_type_order=checkpoint["edge_type_order"],
    ).to(torch.device(device))
    model.load_state_dict(checkpoint["model_state"])
    model.eval()
    return model, config


# ---------------------------------------------------------------------------
# Single-incident inference (kept for ML-5/ML-6)
# ---------------------------------------------------------------------------

@torch.no_grad()
def predict_root(model: RootAlarmGNN, graph, device: str = "cpu") -> tuple[int, float]:
    """Predict the root alarm of a single incident graph.

    Returns ``(alarm_local_index, logit)`` — the argmax alarm and its score.
    """
    model.eval()
    device = torch.device(device)
    batch = Batch.from_data_list([graph]).to(device)
    fwd = model(batch)
    logit = fwd["root_logits"]
    idx = int(logit.argmax())
    return idx, float(logit[idx])
