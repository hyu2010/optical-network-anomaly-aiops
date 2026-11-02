"""Command-line interface for the GNN training pipeline (Design/11 §8).

Three subcommands, all operating on the per-incident graphs produced by
``simulator.ml.dataset``:

    python -m simulator.ml build    [--max-incidents N] [--with-rule-edges]
    python -m simulator.ml inspect  --incident 1 [--with-rule-edges]
    python -m simulator.ml validate [--max-incidents 50]
    python -m simulator.ml train    [--max-incidents N] [--epochs 200]
    python -m simulator.ml train-split [--epochs 60] [--ablation-dt]   # ML-3

* ``build``    — discover vocabularies, then build + cache one ``HeteroData``
                 per incident. This is the normal ML-1 step that populates
                 ``data/ml/cache/``.
* ``inspect``  — print a human-readable summary of a single incident's graph
                 (node counts, edge kinds, the root alarm URI) so the first
                 graph can be eyeballed before any training.
* ``validate`` — the ML-1 *exit criterion*: build N incidents, round-trip
                 CSV -> HeteroData, and assert node/edge counts on incident 1
                 against a hand-computed reference. Exits non-zero on failure.

All three default to reading ``data/kg/`` and ``data/incidents.db`` relative to
the repo root, and cache under ``data/ml/cache/``.
"""
from __future__ import annotations

import argparse
import os
import sys

from .dataset import (
    ALARM_NODE_TYPE,
    DEFAULT_DB,
    DEFAULT_KG_DIR,
    build_dataset,
    build_feature_bundle,
    incident_ids,
    load_inputs,
)
from .features import save_feature_bundle

# Default on-disk cache for built graphs + the cached feature bundle.
DEFAULT_CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ml", "cache")


def _edge_summary(g) -> str:
    """Render a graph's typed edges as a sorted, human-readable bullet list.

    Each line: ``(src_type) --predicate--> (dst_type) : <N> edges``. Sorted so
    the output is diff-stable across runs (otherwise the dict iteration order
    would make comparisons noisy).
    """
    lines = []
    for (st, p, dt), e in g.edge_index_dict.items():
        lines.append(f"    ({st}) --{p}--> ({dt}) : {e.size(1)} edges")
    return "\n".join(sorted(lines))


def cmd_inspect(args) -> int:
    """``inspect`` — build one incident's graph on the fly and print it.

    Deliberately ignores the cache (always rebuilds) so you always see the
    current state of the KG/DB. Useful during development to eyeball a graph
    after a schema or feature change.
    """
    # Imported lazily so `build`/`validate` don't pay for the heavier imports
    # unless inspect is actually run.
    from .dataset import build_incident_graph, TopologyIndex

    alarm_csv, topo_csv, manifest = load_inputs()
    fb = build_feature_bundle()
    topo = TopologyIndex(topo_csv, fb)

    g = build_incident_graph(args.incident, alarm_csv, manifest,
                             topo.edge_tensors(), fb, with_rule_edges=args.with_rule_edges)

    print(f"\n=== incident {args.incident} ===")
    # Alarm-node block: count + how many carry the root label.
    print(f"alarm nodes : {g[ALARM_NODE_TYPE].num_nodes}  "
          f"(root count = {int(g[ALARM_NODE_TYPE].y_root.sum())})")
    # The dense feature matrix shape (rows = #alarms, cols = dense feature dim).
    print(f"alarm.x     : {tuple(g[ALARM_NODE_TYPE].x.shape)}  (dense features)")
    # name_idx is what the model embeds (one int per alarm -> nn.Embedding row).
    print(f"name_idx    : {tuple(g[ALARM_NODE_TYPE].name_idx.shape)}  "
          f"-> {g[ALARM_NODE_TYPE].name_idx.tolist()[:8]}...")
    # Positive correlation pairs (the correlation head's in-incident positives).
    print(f"corr_pos    : {g[ALARM_NODE_TYPE].corr_pos.shape[0]} positive pairs")
    # Topology node types present + their (global) counts.
    print(f"topo types  : {[ (t, g[t].num_nodes) for t in g.node_types if t not in (ALARM_NODE_TYPE,'incident') ]}")
    # Super-node: fault-class label + the two temporal flags.
    print(f"fault label : {int(g['incident'].y_fault[0])}  "
          f"(allclose={int(g['incident'].x[0,0])}, within6={int(g['incident'].x[0,1])})")
    print("edges:")
    print(_edge_summary(g))
    # Finally, which alarm URI is the labelled root (sanity check the join).
    print("\nroot alarm URI:",
          [g[ALARM_NODE_TYPE].uri[i] for i in range(g[ALARM_NODE_TYPE].num_nodes)
           if g[ALARM_NODE_TYPE].y_root[i] == 1])
    return 0


def cmd_build(args) -> int:
    """``build`` — discover vocabularies, cache them, and build + cache graphs.

    Selects only the non-suppressed incidents (each fault is stored once raw
    and once L1-suppressed; building both would double-count — Design/00 §3.7).
    """
    manifest = load_inputs()[2]
    # Take the raw (suppressed=False) incidents, optionally capped.
    ids = incident_ids(manifest, suppressed=False)[: (args.max_incidents or 10 ** 9)]

    regime = "rule" if args.with_rule_edges else "pure"
    print(f"building {len(ids)} incident graphs (regime={regime}) ...")

    # Discover + cache the vocabularies first, so the model layer can reload
    # the exact same feature bundle later.
    fb = build_feature_bundle(args.kg_dir, args.db)
    save_feature_bundle(fb, os.path.join(args.cache_dir, "features.json"))
    print(f"vocab: alarm_names={len(fb.alarm_names)} layers={len(fb.layers)} "
          f"severities={len(fb.severities)} fault_types={len(fb.fault_types)} "
          f"topo_types={sorted(fb.topo.by_type)}")

    build_dataset(ids, fb=fb, cache_dir=args.cache_dir, with_rule_edges=args.with_rule_edges)
    print(f"done -> {args.cache_dir}")
    return 0


def cmd_validate(args) -> int:
    """``validate`` — the ML-1 exit-criterion checks (Design/11 §9 ML-1).

    Three assertions:
      (1) every built graph has >=1 alarm and exactly one root label;
      (2) incident 1 (the worked-example family) has alarms, a single root, and
          the anchor edge kinds we expect (AtNE / IsLayer / AtSpan);
      (3) topology node ids are global — the same span count in two incidents.
    Prints PASS/FAIL and returns 0 on success, 1 on failure.
    """
    alarm_csv, topo_csv, manifest = load_inputs()
    ids = incident_ids(manifest, suppressed=False)[: (args.max_incidents or 50)]
    print(f"validating on {len(ids)} incidents ...")
    graphs = build_dataset(ids, cache_dir=args.cache_dir)

    ok = True

    # (1) per-graph sanity: alarms present + at least one root label.
    for g in graphs:
        n = g[ALARM_NODE_TYPE].num_nodes
        r = int(g[ALARM_NODE_TYPE].y_root.sum())
        if n == 0:
            print(f"  FAIL incident {g.incident_id}: 0 alarms"); ok = False
        if r == 0:
            # KG label join produced nothing — the incident carries no
            # RootCause triple of any provenance. (build_dataset normally
            # skips these; reaching here means an inconsistency.)
            print(f"  WARN incident {g.incident_id}: 0 root labels"); ok = False

    # (2) incident 1 hand-computed reference: the anchor edges that wiring
    #     depends on must all be present. If IsLayer/AtNE/AtSpan are missing,
    #     something is wrong with the topology join (this is exactly the
    #     layer-node bug we hit during ML-1).
    g1 = next(g for g in graphs if g.incident_id == 1)
    edge_kinds = {k[1] for k in g1.edge_index_dict.keys()}
    required_kinds = {"AtNE", "IsLayer", "AtSpan"} & edge_kinds
    assert g1[ALARM_NODE_TYPE].num_nodes > 0, "incident 1 has no alarm nodes"
    assert int(g1[ALARM_NODE_TYPE].y_root.sum()) >= 1, "incident 1 must have at least one root"
    assert required_kinds, f"incident 1 missing anchor edges (has {sorted(edge_kinds)})"
    print(f"  incident 1: {g1[ALARM_NODE_TYPE].num_nodes} alarms, "
          f"edge kinds present: {sorted(required_kinds)} OK")

    # (3) topology global-id stability: the same topology type has the same node
    #     count in every incident (because topology is shared/global, §3.1).
    have_two = [g for g in graphs if g.incident_id in (1, 2)]
    if len(have_two) == 2 and "span" in have_two[0].node_types:
        print(f"  topology shared: span nodes = {have_two[0]['span'].num_nodes} "
              f"(identical across incidents) OK")

    print("ML-1 VALIDATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_train(args) -> int:
    """``train`` — ML-2 overfit sanity check (root head only).

    Loads cached graphs, trains the backbone + root head, and reports the final
    root accuracy. Exit criterion (Design/11 §9 ML-2): loss descends and root
    accuracy climbs high (intentional overfit — generalisation is ML-3).
    """
    from .dataset import build_dataset
    from .features import build_feature_bundle, load_feature_bundle
    from .train import TrainConfig, train_root_head

    # Prefer the cached feature bundle (matches the graphs); fall back to a
    # fresh discovery if the cache is missing.
    fb_path = os.path.join(args.cache_dir, "features.json")
    fb = load_feature_bundle(fb_path) if os.path.exists(fb_path) else build_feature_bundle()

    manifest = load_inputs()[2]
    ids = incident_ids(manifest, suppressed=False)[: (args.max_incidents or 50)]
    print(f"loading {len(ids)} cached graphs (regime={'rule' if args.with_rule_edges else 'pure'}) ...",
          file=sys.stderr)
    graphs = build_dataset(ids, fb=fb, cache_dir=args.cache_dir,
                           with_rule_edges=args.with_rule_edges, progress=False)

    cfg = TrainConfig(
        epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
        hidden=args.hidden, name_dim=args.name_dim, dropout=args.dropout,
        seed=args.seed, log_every=max(1, args.epochs // 10), device=args.device,
    )
    print(f"training: {cfg.epochs} epochs, batch={cfg.batch_size}, "
          f"hidden={cfg.hidden}, device={cfg.device}", file=sys.stderr)

    model, hist = train_root_head(graphs, fb, cfg)
    final_loss = hist.loss[-1]
    final_acc = hist.acc[-1]
    best_acc = max(hist.acc)
    print(f"\nML-2 RESULT: final_loss={final_loss:.4f}  final_root_acc={final_acc:.3f}  "
          f"best_root_acc={best_acc:.3f}")
    print("ML-2 SANITY:", "PASS" if final_acc >= 0.95 else "CHECK")
    return 0


def _zeroed_copy(g):
    """A shallow-cloned graph whose alarm time features (last 2 cols of x:
    Δt, is_first) are zeroed — for the Δt ablation. Non-destructive: the cached
    graph objects are never mutated."""
    import copy
    g2 = copy.copy(g)               # shallow: shares stores, so clone the alarm x
    g2["alarm"].x = g["alarm"].x.clone()
    g2["alarm"].x[:, -2:] = 0.0
    return g2


def cmd_train_split(args) -> int:
    """``train-split`` — ML-3 generalizing run (two heads, real split).

    Builds a stratified 70/15/15 split, trains the backbone + root + correlation
    heads with multi-task loss + cosine schedule + early stopping, then reports
    honest train/val/test root accuracy + pair-correlation F1.
    """
    from .dataset import build_dataset
    from .features import build_feature_bundle, load_feature_bundle
    from .splits import make_split, save_split, load_split
    from .train import MTLConfig, train_mtl, evaluate, save_run

    fb_path = os.path.join(args.cache_dir, "features.json")
    fb = load_feature_bundle(fb_path) if os.path.exists(fb_path) else build_feature_bundle()

    manifest = load_inputs()[2]
    all_ids = incident_ids(manifest, suppressed=False)

    # build ALL graphs once (cached), then slice by split
    print(f"loading {len(all_ids)} cached graphs ...", file=sys.stderr)
    all_graphs = build_dataset(all_ids, fb=fb, cache_dir=args.cache_dir,
                               with_rule_edges=False, progress=False)
    id2g = {g.incident_id: g for g in all_graphs}

    # restrict the split to ids that actually produced a graph (some incidents
    # in the contaminated demo DB have no alarm triples and were skipped).
    manifest = manifest[manifest["incident_id"].isin(id2g)]

    split_path = os.path.join(args.cache_dir, "splits.json")
    if os.path.exists(split_path) and not args.resplit:
        sp = load_split(split_path)
        print(f"reusing split {split_path}", file=sys.stderr)
    else:
        sp = make_split(manifest, seed=args.seed)
        save_split(sp, split_path)
        print(f"new split -> {split_path}", file=sys.stderr)
    print(f"split: train={len(sp.train)} val={len(sp.val)} test={len(sp.test)}", file=sys.stderr)

    tr = [id2g[i] for i in sp.train if i in id2g]
    va = [id2g[i] for i in sp.val if i in id2g]
    te = [id2g[i] for i in sp.test if i in id2g]

    cfg = MTLConfig(
        epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
        hidden=args.hidden, name_dim=args.name_dim, dropout=args.dropout,
        seed=args.seed, lambda_corr=args.lambda_corr, patience=args.patience,
        device=args.device,
    )
    print(f"training (ML-3): {cfg.epochs} epochs, batch={cfg.batch_size}, "
          f"λ_corr={cfg.lambda_corr}, device={cfg.device}", file=sys.stderr)

    model, hist = train_mtl(tr, va, fb, cfg)

    # final test-set evaluation (held out entirely from training/model selection)
    te_metrics = evaluate(model, te, fb, cfg) if te else {"root_accuracy": 0.0}

    print(f"\nML-3 RESULT (best val root_acc={hist.best_val_root_acc:.3f} @ epoch {hist.best_epoch}):")
    print(f"  train root_acc : {hist.train_root_acc[-1]:.3f}")
    print(f"  val   root_acc : {hist.val_root_acc[-1]:.3f}")
    print(f"  val   corr_f1  : {hist.val_corr_f1[-1]:.3f}")
    print(f"  TEST  root_acc : {te_metrics['root_accuracy']:.3f}")
    print(f"  TEST  corr_f1  : {te_metrics['corr_f1']:.3f}")

    # ---- §8 run checkpointing: persist weights + config + metrics ----
    run_dir = os.path.join(args.cache_dir, "runs", args.run_name)
    metrics = {
        "best_val_root_acc": hist.best_val_root_acc,
        "best_epoch": hist.best_epoch,
        "train_root_acc": hist.train_root_acc[-1] if hist.train_root_acc else None,
        "val_root_acc": hist.val_root_acc[-1] if hist.val_root_acc else None,
        "val_corr_f1": hist.val_corr_f1[-1] if hist.val_corr_f1 else None,
        "test_root_acc": te_metrics.get("root_accuracy"),
        "test_corr_f1": te_metrics.get("corr_f1"),
        "n_train": len(tr), "n_val": len(va), "n_test": len(te),
    }
    save_run(model, cfg, metrics, run_dir, extra={
        "run_name": args.run_name,
        "split_file": os.path.join(args.cache_dir, "splits.json"),
        "label_source": "kg:RootCause(source=alarm|rule fallback)",
        "with_rule_edges": False,
    })
    print(f"  checkpoint    : {run_dir}/best.pt", file=sys.stderr)

    # Δt ablation (§10 Risk #3): zero out the is_first/dt features and re-train,
    # to check the accuracy isn't a "earliest = root" time shortcut.
    if args.ablation_dt:
        print("\n--- Δt ablation (zero time features) ---", file=sys.stderr)
        # Zero the time columns on COPIES (never mutate the cached graphs —
        # the main model above was trained on the originals).
        tr_ab = [_zeroed_copy(g) for g in tr]
        va_ab = [_zeroed_copy(g) for g in va]
        te_ab = [_zeroed_copy(g) for g in te]
        ablation_model, ablation_hist = train_mtl(tr_ab, va_ab, fb, cfg)
        ab_te = evaluate(ablation_model, te_ab, fb, cfg)
        print(f"  Δt-off  val root_acc : {ablation_hist.best_val_root_acc:.3f}")
        print(f"  Δt-off  TEST root_acc: {ab_te['root_accuracy']:.3f}")
        print(f"  Δt-on   TEST root_acc: {te_metrics['root_accuracy']:.3f}  "
              f"(gap={te_metrics['root_accuracy'] - ab_te['root_accuracy']:+.3f})")

        # checkpoint the ablation run as its own run dir
        ab_dir = os.path.join(args.cache_dir, "runs", args.run_name + "_dt_off")
        save_run(ablation_model, cfg, {
            "best_val_root_acc": ablation_hist.best_val_root_acc,
            "best_epoch": ablation_hist.best_epoch,
            "test_root_acc": ab_te.get("root_accuracy"),
            "test_corr_f1": ab_te.get("corr_f1"),
            "ablation": "dt_zeroed",
        }, ab_dir, extra={"run_name": args.run_name + "_dt_off",
                          "ablation": "dt_zeroed"})
        print(f"  checkpoint    : {ab_dir}/best.pt", file=sys.stderr)

    return 0


def main(argv=None) -> int:
    """Build the argparse subcommand dispatch and run the selected command."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # build -----------------------------------------------------------------
    b = sub.add_parser("build", help="build + cache HeteroData per incident")
    b.add_argument("--max-incidents", type=int, default=None,
                   help="cap the number of incidents built (default: all raw)")
    b.add_argument("--with-rule-edges", action="store_true",
                   help="rule-augmented regime: feed rule:R* Correlated as input (§5.5)")
    b.add_argument("--cache-dir", default=os.path.abspath(DEFAULT_CACHE))
    b.add_argument("--kg-dir", default=DEFAULT_KG_DIR)
    b.add_argument("--db", default=DEFAULT_DB)
    b.set_defaults(func=cmd_build)

    # inspect ---------------------------------------------------------------
    i = sub.add_parser("inspect", help="print one incident's graph")
    i.add_argument("--incident", type=int, default=1)
    i.add_argument("--with-rule-edges", action="store_true")
    i.set_defaults(func=cmd_inspect)

    # validate --------------------------------------------------------------
    v = sub.add_parser("validate", help="ML-1 exit-criterion checks")
    v.add_argument("--max-incidents", type=int, default=50)
    v.add_argument("--cache-dir", default=os.path.abspath(DEFAULT_CACHE))
    v.set_defaults(func=cmd_validate)

    # train -----------------------------------------------------------------
    t = sub.add_parser("train", help="ML-2 overfit sanity check (root head only)")
    t.add_argument("--max-incidents", type=int, default=50)
    t.add_argument("--epochs", type=int, default=200)
    t.add_argument("--batch-size", type=int, default=8)
    t.add_argument("--lr", type=float, default=5e-4)
    t.add_argument("--hidden", type=int, default=64)
    t.add_argument("--name-dim", type=int, default=32)
    t.add_argument("--dropout", type=float, default=0.2)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--device", default="cpu")
    t.add_argument("--with-rule-edges", action="store_true")
    t.add_argument("--cache-dir", default=os.path.abspath(DEFAULT_CACHE))
    t.set_defaults(func=cmd_train)

    # train-split (ML-3) ----------------------------------------------------
    ts = sub.add_parser("train-split", help="ML-3 generalizing run (two heads, real split)")
    ts.add_argument("--epochs", type=int, default=60)
    ts.add_argument("--batch-size", type=int, default=16)
    ts.add_argument("--lr", type=float, default=5e-4)
    ts.add_argument("--hidden", type=int, default=64)
    ts.add_argument("--name-dim", type=int, default=32)
    ts.add_argument("--dropout", type=float, default=0.2)
    ts.add_argument("--lambda-corr", type=float, default=0.5)
    ts.add_argument("--patience", type=int, default=10)
    ts.add_argument("--seed", type=int, default=0)
    ts.add_argument("--device", default="cpu")
    ts.add_argument("--resplit", action="store_true", help="force a new split")
    ts.add_argument("--ablation-dt", action="store_true",
                    help="also run the Δt time-shortcut ablation (§10 Risk #3)")
    ts.add_argument("--cache-dir", default=os.path.abspath(DEFAULT_CACHE))
    ts.add_argument("--run-name", default="rgcn_run",
                    help="run-directory name under <cache-dir>/runs/")
    ts.set_defaults(func=cmd_train_split)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
