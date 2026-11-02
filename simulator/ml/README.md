# simulator/ml — GNN for root-alarm detection + alarm correlation

The ML track of the AIOps pipeline (Design doc: `Design/11_gnn_training_plan.md`).
It consumes the Knowledge-Graph CSVs the simulator produces (`data/kg/*.csv`) and
`incidents.db`, and trains a graph neural network that answers, per incident:

1. **Which alarm is the root?** — the *root head*: scores every alarm, picks one.
2. **Which alarms belong together?** — the *correlation head*: scores every alarm
   pair ("same fault group?").

Everything runs on CPU by default; no GPU required.

```
data/kg/*.csv + incidents.db ──▶ dataset.py ──▶ per-incident HeteroData graphs
                                                      │
                                                      ▼
                                              model.py (R-GCN + 2 heads)
                                                      │
                                    train.py (multi-task loss, schedule)
                                                      │
                                                      ▼
                              data/ml/cache/runs/<name>/{best.pt, config.json, metrics.json}
```

---

## 0. Prerequisites

* The repo venv with `torch` + `torch_geometric` installed
  (`.venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu`
  then `.venv/bin/python -m pip install torch_geometric`).
* A populated dataset: `data/kg/*.csv` (built by `python -m simulator.kg build`)
  and `data/incidents.db` (built by `python -m simulator.simulator.generate_dataset`).
  Run `./setup_data.sh` first if `data/kg/` is empty.

Run everything **from the repo root** so `simulator.*` imports resolve.

---

## 1. Quick start (train a model in four commands)

```bash
# 1) sanity: build graphs for 50 incidents and check they round-trip
.venv/bin/python -m simulator.ml validate --max-incidents 50

# 2) overfit sanity check (ML-2): should reach ~100% root accuracy on its
#    own training incidents within ~40 epochs — proves the wiring works
.venv/bin/python -m simulator.ml train --max-incidents 50 --epochs 60

# 3) the real run (ML-3): stratified train/val/test split, both heads,
#    cosine schedule, early stopping; ~10–30 min on CPU
.venv/bin/python -m simulator.ml train-split --epochs 60 --batch-size 16 \
    --run-name my_first_run

# 4) also run the Δt ablation (are the time features a shortcut?) and save
#    it as its own run directory
.venv/bin/python -m simulator.ml train-split --epochs 60 --run-name my_first_run \
    --ablation-dt
```

After step 3 you'll see a summary like:

```
ML-3 RESULT (best val root_acc=0.980 @ epoch 9):
  train root_acc : 0.992
  val   root_acc : 0.980
  val   corr_f1  : 0.577
  TEST  root_acc : 1.000
  TEST  corr_f1  : 0.415
  checkpoint    : data/ml/cache/runs/my_first_run/best.pt
```

and the trained model + config + metrics are stored under
`data/ml/cache/runs/my_first_run/` (see §5).

---

## 2. Command reference

All commands: `.venv/bin/python -m simulator.ml <subcommand>` from the repo root.

| Subcommand | What it does | Key flags |
|---|---|---|
| `build` | Discover vocabularies, build + cache one graph per incident (`data/ml/cache/incident_<id>_<regime>.pt`). Also writes `features.json`. | `--max-incidents N`, `--with-rule-edges` (rule-augmented regime), `--cache-dir`, `--kg-dir`, `--db` |
| `inspect` | Print one incident's graph — node counts, every edge kind with counts, the labelled root alarm URI. Best way to eyeball what the model sees. | `--incident 1`, `--with-rule-edges` |
| `validate` | ML-1 exit-criterion checks: every graph has ≥1 root label, anchor edges present (`AtNE`/`AtSpan`/`IsLayer`), topology node ids global across incidents. | `--max-incidents 50` |
| `train` | **ML-2 overfit sanity check** — root head only, no split, no correlation head. Deliberately overfits a small set to prove the pipeline. | `--max-incidents 50`, `--epochs 200`, `--batch-size 8`, `--lr`, `--hidden`, `--seed`, `--device` |
| `train-split` | **ML-3 generalizing run** — stratified 70/15/15 split, both heads, multi-task loss, warmup+cosine schedule, early stopping on val root accuracy, test-set evaluation, §8 checkpointing. | `--epochs 60`, `--batch-size 16`, `--lambda-corr 0.5`, `--patience 10`, `--run-name`, `--resplit`, `--ablation-dt`, `--seed`, `--device` |

Examples:

```bash
# eyeball incident 1: 60+ alarms, every edge kind, the labelled root URI
.venv/bin/python -m simulator.ml inspect --incident 1

# build ALL incident graphs up front (train-split does this itself if needed)
.venv/bin/python -m simulator.ml build

# hyperparameter variation
.venv/bin/python -m simulator.ml train-split --epochs 80 --lambda-corr 1.0 \
    --hidden 96 --run-name rgcn_l1_h96

# force a fresh random split (otherwise splits.json is reused)
.venv/bin/python -m simulator.ml train-split --resplit --run-name new_split_run
```

---

## 3. Using a trained model (inference)

There is no `predict` subcommand yet; load a checkpoint from Python:

```python
from simulator.ml.train import load_run, predict_root
from simulator.ml.dataset import build_dataset

model, config = load_run("data/ml/cache/runs/rgcn_full_v2")   # rebuilds weights + vocab
graph = build_dataset([101], cache_dir="data/ml/cache")[0]     # one incident
idx, logit = predict_root(model, graph)                        # → argmax alarm index + score
print(graph["alarm"].uri[idx])          # the predicted root alarm URI
```

`load_run` restores everything needed to rebuild the model faithfully — not
just the weights but the relation vocabulary (`edge_type_order`), the layer
sizes, and the full feature vocabularies (alarm names / layers / severities /
fault types / topology ids). A checkpoint is self-describing.

---

## 4. Code architecture

Six modules, one responsibility each. Data flows strictly downward:

```
features.py   vocabulary discovery + feature encoders (no torch import needed)
    ▲
dataset.py    CSV + incidents.db → one PyG HeteroData per incident (the graphs)
    ▲
splits.py     train/val/test splitting + cross-incident negative sampling
    ▲
model.py      the network: input encoders, R-GCN backbone, two heads, metrics
    ▲
train.py      the training loops (overfit + generalizing) + checkpointing
    ▲
__main__.py   the CLI (build / inspect / validate / train / train-split)
```

### 4.1 `features.py` — vocabularies and encoders

Discovers every categorical vocabulary **from the data** rather than hardcoding
it: 95 alarm names, 13 layers, 4 severities, 11 fault types, per-type topology
ids (ne/board/port/span/och/otu/odu/fpos/svc + layer). Key classes:

* **`Vocab`** — an insertion-ordered name→index map with an `unknown` fallback
  (index 0), so unseen values at inference never crash.
* **`TopoVocab`** — per-type URI→index maps for topology nodes. The *same*
  NE/span/port keeps the *same* index in every incident's graph, so the model's
  `nn.Embedding` gives it one shared vector (Design/11 §4.2).
* **`FeatureBundle`** — all vocabularies + a schema signature, JSON-serializable
  so a checkpoint can reload the exact vocab it was trained with.
* `build_alarm_features()` — the per-alarm dense vector:
  `[layer one-hot (13) | severity one-hot (4) | Δt (1) | is_first (1)]`.
  The alarm-name is *not* in this vector — the model embeds it separately from
  `name_idx` via its own learned `nn.Embedding`.
* `build_feature_bundle()` — the one-time scan of `data/kg/` + `incidents.db`.

The predicate vocabulary (which KG predicates are INPUT edges vs held-out
labels) is also pinned here (`INPUT_*_PREDICATES`, `TARGET_PREDICATES`).

### 4.2 `dataset.py` — building the graphs

One PyG `HeteroData` **per incident**, the unit of training/eval:

* **alarm nodes** — local index 0..N-1. Carry the dense feature vector,
  `name_idx`, the `y_root` label, `y_secondary`, the `corr_pos` positive-pair
  index, and the URIs (for debugging/explanation).
* **topology nodes** — the *full global set* in every graph (it's small and
  shared): ne(6) board(36) port(82) span(16) och(6) otu(16) odu(6) fpos(8)
  svc(6) layer(13). They have **no features and no labels** — they are context
  that routes messages (see Design/11 §5.6).
* **incident super-node** — one node holding the two temporal flags
  (`AllClose`/`AllWithinSix`) and the graph-level fault-type label.
* **Edges, typed `(src_type, predicate, dst_type)`** — three families:
  the topology spine (`Hosts`, `Connected`, `CarriedBy`, …, identical across
  incidents, built once by `TopologyIndex`), per-alarm anchors (`AtNE`,
  `AtBoard`, `AtPort`, `AtSpan`, `IsLayer`, `OnPipe`, `AtChannel`), and
  alarm↔alarm locality (`OnSameNE`, `OnSameBoard`, `OnSamePort`,
  `OnSameContainer`, `SameSpan`). Every input edge gets a `_rev` reverse edge
  so messages flow both ways.
* **Labels are held out of the edge set**: `RootCause` becomes the `y_root`
  node label; `Correlated` becomes the `corr_pos` pair index; `Secondary`
  becomes `y_secondary`. The pure-GNN regime never feeds them as input.

**Where the root label comes from** (`build_incident_graph`): the KG's
`RootCause` triples — preferring `source=alarm` (the simulator's ground truth)
when present, falling back to `source=rule` (the rule reasoner's pick, which is
what the current KG carries). `incidents.db` is consulted only for *features*
(alarm name, severity, layer, `t_observed_ms`) — never for the label. This
keeps the DB a pure alarm dump with no answer key.

**Caching**: every graph is saved once as `data/ml/cache/incident_<id>_<regime>.pt`
and reloaded on later runs (`<regime>` = `pure` | `rule`), so the large CSVs are
parsed once, not per epoch. Delete the `.pt` files to force a rebuild.

### 4.3 `splits.py` — splits + the negative sampler

* `make_split()` — stratified 70/15/15 by fault type, **grouping whole
  `(fault_type, fault_target)` sets into the same split**. This matters: the
  dataset contains many near-duplicate cascades (same fault on the same span);
  without grouping, the model would memorise a cascade in train and "predict"
  its twin in test — leakage. Persisted as `data/ml/cache/splits.json`.
* `sample_pairs_for_batch()` — the cross-incident negative sampler (Design/11
  §5.3). Within one incident every alarm pair is a positive, so the learning
  signal comes from negatives: random pairs drawn from **two different
  incidents in the same mini-batch**, at ~3:1 neg:pos, re-rolled every step.
* `offset_pos_pairs_for_batch()` — PyG concatenates each graph's `corr_pos`
  along dim 0 but does **not** add the per-graph alarm offset to the pair
  indices; this helper fixes that using the batch's slice dict.

### 4.4 `model.py` — the network

`RootAlarmGNN` = **input encoders → 2× R-GCN → two heads**:

```
   alarm node   [layer|sev|Δt|is_first] (19) ‖ name_emb(32)  ──Linear──▶ 64
   topology     nn.Embedding per type (global id → 64)                 ──▶ 64
   incident     [AllClose, AllWithinSix] (2)                ──Linear──▶ 64
                                        │
        homogenised graph (to_homogeneous): all typed edges → relation ids
                                        │
        RGCNConv(64→64, R) → GELU → dropout → RGCNConv(64→64, R)      ← backbone
                                        │
        z_alarm = alarm rows of the output                             │
           ┌──────────────────────────────┴───────────────────────────┐
   root_head: MLP(64→32→1)                                  corr_head: MLP(128→64→1)
   one logit per ALARM                                       one logit per PAIR [z_i‖z_j]
```

Two design points worth knowing:

* **Relation vocabulary**: not every incident contains every edge type, so the
  convs are sized against the **dataset-wide union** (`relation_vocab()`), and
  each batch's relation ids are remapped into that canonical space
  (`_remap_relations`). Construct the model with both `num_relations` and
  `edge_type_order` (as `train.py` does) — the lazy-init path exists but is
  only a fallback.
* **One backbone, two heads** (multi-task): the heads share no weights, but
  both losses backprop into the same R-GCN and embeddings — grouping pressure
  and root pressure co-train one representation.

Metrics live here too: `root_loss` (pos-weighted BCE, ~50:1 imbalance),
`root_accuracy` (argmax-over-incident against a possibly multi-root label set),
`corr_loss_from_pairs`, `pair_correlation_f1`.

### 4.5 `train.py` — training + checkpointing

Two loops:

* `train_root_head()` — the ML-2 overfit loop (root head only, no split).
  A sanity check, not a way to produce models.
* `train_mtl()` — the real trainer: multi-task loss
  `L = L_root + λ_corr·L_corr`, linear warmup → cosine decay, gradient
  clipping, early stopping on **val root accuracy** (patience), best-epoch
  state restore. `evaluate()` gives held-out root acc + pair-correlation P/R/F1.
  ⚠️ known limitation: early stopping keys on root accuracy only, which can
  cut training while the correlation head is still improving (see "open
  items" below).

**Checkpointing** (`save_run` / `load_run`, Design/11 §8): each run writes

```
data/ml/cache/runs/<run-name>/
├── best.pt       weights + edge_type_order + num_relations + hidden/name_dim
│                 + the full feature-bundle JSON (a self-describing checkpoint)
├── config.json   hyperparameters, split file, label provenance, flags
└── metrics.json  train/val/test root acc + corr F1, best epoch, split sizes
```

`load_run()` rebuilds the model exactly — verified to give byte-identical
predictions across reloads.

### 4.6 `__main__.py` — the CLI

Thin argparse dispatch over the five subcommands (table in §2). Nothing else
lives here; all logic is importable from the modules.

---

## 5. Artifacts & what's gitignored

```
data/ml/cache/
├── incident_<id>_<regime>.pt   ← regenerable graph cache (gitignored)
├── features.json               ← cached vocabularies   (gitignored)
├── splits.json                 ← the pinned split      (gitignored)
└── runs/<name>/                ← TRAINED MODELS        (tracked in git)
```

The graph cache is derived data — delete it any time and `build`/`train-split`
regenerate it from `data/kg/`. The `runs/` directories are the products you
keep (and, per the repo's `.gitignore`, the only part of `data/ml/cache` that
is committed).

---

## 6. Design doc map

Everything here implements `Design/11_gnn_training_plan.md`:

| Code | Design section |
|---|---|
| node/edge typing, held-out labels | §3 (incl. the 3-alarm worked example §3.5) |
| feature vectors | §4 |
| R-GCN backbone, root/correlation heads, multi-task loss | §5 |
| splits, optimizer, schedule, early stopping | §6 |
| metrics (root_accuracy, pair F1) | §7 |
| module layout, run directories, caching | §8 |
| ML-1…ML-6 phase mapping | §9 |

Current status: **ML-1/ML-2/ML-3 complete** (on the 2880-incident dataset,
rule-derived labels: test root acc 100%, corr F1 41.5%). Open items: the
correlation head's recipe (early-stop criterion, λ_corr, hard negatives,
SameIncident positives), ground-truth (`source=alarm`) labels for a true
GNN-vs-rules comparison, then ML-4 (grid search) → ML-5 (explanations) →
ML-6 (live streaming inference).

Important caveat when reading any accuracy number: with `source=rule` labels
the GNN is learning to *reproduce the rule reasoner* — its ceiling is the
rules themselves. The "GNN vs 99.1% rules baseline" question needs the
simulator's ground-truth key emitted into the KG first.
