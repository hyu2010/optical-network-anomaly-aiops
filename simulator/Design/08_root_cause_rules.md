# Root-Cause Inference Rules

Status: **v1 — derived from the v3 simulator's behavior and the two reference files** at `Library/Alarm correlation logical rules.txt` and `Library/RuleFormat.txt`.

This document is the rule-set the RCA pipeline (or a hand-written reasoner, or a GNN's positive-edge supervision) uses to answer **"given this set of alarms, which one is the root cause and how are the rest derived from it?"**.

Every rule below is justified by a specific behavior of the v3 simulator — most can be cross-checked against `data/incidents.db` with one SQL query. Rules are grouped by the kind of evidence they consume; later sections combine them.

---

## 1. Predicates

The vocabulary the rules speak. Every predicate is either directly a column of `alarms` / `incidents` / a YANG entity, or derivable from one or two of those by simple lookup.

### 1.1 Alarm attributes (per row in `alarms`)

| Predicate                | Reads                                  | Comment |
|---|---|---|
| `Alarm(a)`               | row exists in `alarms`                 |  |
| `Name(a, X)`             | `alarms.alarm_name`                    | family form, e.g. `OTS_LOS`, `OTU_SSF` |
| `IsLayer(a, L)`          | `alarms.layer`                         | one of OTS, OMS, OCH, OTU, ODU, SDH, FEC, equipment, environment, client, protection, auto-power, clock, management |
| `Severity(a, S)`         | `alarms.severity`                      | Critical / Major / Minor / Warning |
| `Container(a, C)`        | `alarms.container_id`                  | `span_*`, `otu_*`, `och_*`, `odu_*`, port uuid, or board uuid |
| `AtNE(a, N)`             | `alarms.ne`                            |  |
| `AtBoard(a, B)`          | `alarms.board`                         | inventory board uuid (may be empty) |
| `AtPort(a, P)`           | `alarms.port`                          | inventory port uuid (may be empty) |
| `Group(a, G)`            | `alarms.group_id`                      | `R1`, `R2`, … (which root this alarm descends from) |
| `T_phys(a, t)`           | `alarms.t_offset_ms`                   | physical propagation time (ground truth) |
| `T_obs(a, t)`            | `alarms.t_observed_ms`                 | NMS-observed time (after poll-window jitter) — **the input the rules see at inference time** |

### 1.2 Time-window predicates (per `Library/RuleFormat.txt`)

| Predicate              | Definition                                 |
|---|---|
| `Close(a1, a2)`        | `\|T_obs(a1) − T_obs(a2)\| ≤ 5 min`         |
| `WithinSix(a1, a2)`    | `\|T_obs(a1) − T_obs(a2)\| ≤ 6 min`         |
| `Earlier(a1, a2)`      | `T_obs(a1) < T_obs(a2)`                    |

### 1.3 Locality predicates (one alarm vs another)

| Predicate                 | True when                                                                    |
|---|---|
| `OnSameNE(a1, a2)`        | `AtNE(a1, n) ^ AtNE(a2, n)`                                                  |
| `OnSameBoard(a1, a2)`     | `OnSameNE(a1, a2) ^ AtBoard(a1, b) ^ AtBoard(a2, b)`                         |
| `OnSamePort(a1, a2)`      | `OnSameBoard(a1, a2) ^ AtPort(a1, p) ^ AtPort(a2, p)`                        |
| `OnSameContainer(a1, a2)` | `Container(a1, c) ^ Container(a2, c)`                                        |
| `OnSameSpan(a1, a2)`      | `Container(a1, span_X) ^ Container(a2, span_X)` — both bound to the *same* `span_*` |
| `OnSameTunnel(a1, a2)`    | both `Container(a, _)` are the same `och_*` or `odu_*`                       |

### 1.4 Topology predicates (read from the three YANG instance files)

| Predicate                  | Source                                           |
|---|---|
| `IsSpan(c)`                | `c ∈ optical-topo/link/link-id`                   |
| `IsOTULink(c)`             | `c ∈ otn-topo/link/link-id`                       |
| `IsOCh(c)`                 | `c ∈ wdm-tunnels`                                 |
| `IsODU(c)`                 | `c ∈ otn-tunnels`                                 |
| `Connected(n1, n2)`        | `Exists s. IsSpan(s) ^ source(s)=n1 ^ dest(s)=n2` |
| `Carries(otu, span)`       | OTU link's `supporting-link` = `span`              |
| `Traverses(och, otu)`      | OCh tunnel's ERO contains this OTU link           |
| `Traverses(och, span)`     | follows from `Traverses(och, otu) ^ Carries(otu, span)` |
| `RidesOn(odu, och)`        | ODU tunnel's `supporting-tunnel` = OCh tunnel     |
| `ClientService(odu, c)`    | ODU's `nfms:client-service-name` = c              |
| `PeerPort(p1, p2)`         | `p1` is the TX-side TP and `p2` the RX-side TP of the same span |

### 1.5 Inference targets (what the rules conclude)

| Predicate              | Meaning                                              |
|---|---|
| `SameIncident(a1, a2)` | the two alarms belong to one root-cause event       |
| `Correlated(a1, a2)`   | one of R1–R4 fires on the pair (with a weight)      |
| `RootCandidate(a)`     | `a` is plausible as the root of its incident         |
| `RootCause(a)`         | `a` is *the* root of its incident (final pick)       |
| `Secondary(a, r)`      | `a` is a derived alarm caused by root `r`           |

### 1.6 Convention

`^` = AND, `=>` = implies, lower-case `a, b` are alarm variables, upper-case `N, P, B, C` are ground identifiers. Rules with a **weight** (R1–R4 family) contribute additively; rules without a weight are **deterministic** (always fire when their premise is true). When two rules disagree, deterministic beats weighted; among weighted, higher cumulative weight wins.

---

## 2. Correlation rules (the four "R" rules with weights)

Verbatim from `Library/Alarm correlation logical rules.txt`, restated as Datalog. These rules **group alarms into the same incident** — they do not name a root. Each pair gets a confidence score equal to the sum of weights of every rule that fires on it.

### R1 — Time clustering, weight 0.1

> A set of alarms generated within 6 minutes, with any two consecutive alarms ≤ 5 minutes apart, are correlated.

```
Close(a1, a2) ^ WithinSix(a1, a2)
   => Correlated(a1, a2)  [w=0.1]
```

Transitive extension (per `RuleFormat.txt`):

```
Correlated(a1, a2) ^ Close(a2, a3) ^ WithinSix(a1, a3)
   => Correlated(a1, a3)  [w=0.1]
```

### R2 — OTU vs OCh tunnel, weight 0.2

> An OTU-container alarm and an OCh-tunnel alarm are correlated if the OCh signal is carried in that OTU.

```
IsLayer(a1, OTU) ^ IsLayer(a2, OCH)
   ^ Container(a1, otu) ^ Container(a2, och)
   ^ Traverses(och, otu)
   => Correlated(a1, a2)  [w=0.2]
```

### R3 — Two ODU underlay segments of one client service, weight 0.4

> Two ODU alarms are correlated if their ODUs are underlay segments of the same client service.

```
IsLayer(a1, ODU) ^ IsLayer(a2, ODU)
   ^ Container(a1, odu1) ^ Container(a2, odu2)
   ^ ClientService(odu1, c) ^ ClientService(odu2, c)
   ^ odu1 != odu2
   => Correlated(a1, a2)  [w=0.4]
```

In v3 every ODU has `client-service-name = chN` matching its name (1-segment per client), so R3 fires only across multi-segment client services — none in v3 yet.

### R4 — OCh paths share an OTS link, weight 0.3

> Two OCh-tunnel alarms are correlated if their paths have a common OTS link.

```
IsLayer(a1, OCH) ^ IsLayer(a2, OCH)
   ^ Container(a1, och1) ^ Container(a2, och2)
   ^ Traverses(och1, span) ^ Traverses(och2, span)
   ^ och1 != och2
   => Correlated(a1, a2)  [w=0.3]
```

R4 is the **canonical demo** of the v3 simulator: `fiber_cut(span_OMS3_BC)` brings down `ch2` and `ch4` because both OCh paths traverse `span_OMS3_BC`.

---

## 3. Layer-cascade rules (within a port)

These are *deterministic* — they come from `data/alarm_suppression.json` (50 expert-curated edges) plus the optical-layer hierarchy. When two alarms sit at the same physical location and their layers form an upstream→downstream pair, the upstream layer is upstream **causally**.

Layer order (upstream → downstream in the propagation sense): **OTS → OMS → OCH → OTU → SDH/FEC/ODU/OPU → client (Ethernet/GFP)**.

```
IsLayer(a1, OTS) ^ IsLayer(a2, OMS) ^ OnSamePort(a1, a2) ^ Close(a1, a2)
   => Secondary(a2, a1)

IsLayer(a1, OMS) ^ IsLayer(a2, OCH) ^ OnSamePort(a1, a2) ^ Close(a1, a2)
   => Secondary(a2, a1)

IsLayer(a1, OCH) ^ IsLayer(a2, OTU) ^ OnSamePort(a1, a2) ^ Close(a1, a2)
   => Secondary(a2, a1)

IsLayer(a1, OTU) ^ IsLayer(a2, ODU) ^ OnSamePort(a1, a2) ^ Close(a1, a2)
   => Secondary(a2, a1)

IsLayer(a1, OTU) ^ IsLayer(a2, SDH) ^ OnSamePort(a1, a2) ^ Close(a1, a2)
   => Secondary(a2, a1)

IsLayer(a1, ODU) ^ IsLayer(a2, client) ^ OnSamePort(a1, a2) ^ Close(a1, a2)
   => Secondary(a2, a1)
```

These six rules already cover the bulk of the suppression edges in
`data/alarm_suppression.json`. Two important refinements:

```
# OTS_LOS specifically dominates everything at the same port — even bit-error
# alarms on overlying OTU/SDH layers — because LOS implies the bit stream is
# gone, and bit-error counts only have meaning when there IS a stream.
Name(a1, OTS_LOS) ^ OnSamePort(a1, a2) ^ Close(a1, a2) ^ a2 != a1
   => Secondary(a2, a1)

# Same for MUT_LOS (the trunk-port summary alarm).
Name(a1, MUT_LOS) ^ OnSamePort(a1, a2) ^ Close(a1, a2) ^ a2 != a1
   => Secondary(a2, a1)
```

---

## 4. Topology-grounded cascade rules (across ports / spans)

The simulator's `fiber_cut` rule fires `OTS_LOS` at the RX-side port of the cut span, and **also** fires `R_LOS / OTU_SSF` at every downstream OTU's RX port whose underlay touches the same span. Two rules capture this:

```
# Peer-port loss-of-signal: when one side of a span goes dark, the other side
# sees R_LOS within milliseconds.
Name(a1, OTS_LOS) ^ AtPort(a1, p1) ^ Name(a2, R_LOS) ^ AtPort(a2, p2)
   ^ PeerPort(p1, p2) ^ Close(a1, a2)
   => Secondary(a2, a1)

# Underlay-supporting-link cascade: every OTU pipe that runs over a faulted
# span produces an R_LOS / OTU_SSF / OTU_BDI cascade at its endpoints.
Name(a1, OTS_LOS) ^ Container(a1, span)
   ^ IsLayer(a2, OTU) ^ Container(a2, otu)
   ^ Carries(otu, span) ^ Close(a1, a2)
   => Secondary(a2, a1)
```

Per-tunnel propagation: for every ODU that rides on an OCh that traverses the affected span, an `ODU_PM_SSF` arrives at the destination client TP:

```
Name(a1, OTS_LOS) ^ Container(a1, span)
   ^ Name(a2, ODU_PM_SSF) ^ Container(a2, odu)
   ^ Exists och: RidesOn(odu, och) ^ Traverses(och, span)
   ^ Close(a1, a2)
   => Secondary(a2, a1)
```

---

## 5. Equipment & environment rules ("trump cards")

NE-level and board-level events override every alarm at the same NE / board because they cause every other alarm at that location.

```
# Whole-NE power loss: every alarm at the NE in the next minute is secondary.
Name(a1, POWER_ABNORMAL) ^ AtNE(a1, n) ^ AtNE(a2, n) ^ Close(a1, a2) ^ a1 != a2
   => Secondary(a2, a1)

# Single-board power loss: same idea, scoped to one board.
Name(a1, BD_OFFLINE) ^ AtBoard(a1, b) ^ AtBoard(a2, b) ^ Close(a1, a2) ^ a1 != a2
   => Secondary(a2, a1)

# Fan failure → temperature alarms within ~5 minutes on every other board.
Name(a1, FAN_FAIL) ^ AtNE(a1, n)
   ^ Name(a2, TEMP_OVER) ^ AtNE(a2, n)
   ^ Close(a1, a2)
   => Secondary(a2, a1)

# Hardware failure on a board: every other alarm on the same board is secondary.
Name(a1, HARD_BAD) ^ AtBoard(a1, b) ^ AtBoard(a2, b) ^ Close(a1, a2) ^ a1 != a2
   => Secondary(a2, a1)
```

---

## 6. Polling-delay-aware rules (the v3 twist)

Per the simulator's `engine.apply_polling_delay()`, observed times are layer-keyed:
OTS / OMS poll every ~5 s, OTU / SDH every ~30 s, environment every ~5 min. Empirically, **the root-cause alarm is NOT the chronologically first observed alarm in 80.6 % of generated incidents** (see `docs/02_simulator_user_guide.md` §2). Time alone is therefore a weak feature; topology + layer must dominate.

Two rules formalise the correction:

```
# Lower-layer alarms are root candidates even when they arrive after
# upper-layer alarms in the OBSERVED timeline.  Justification: lower
# layers (OTS/OMS) poll FAST so they tend to land early in observed
# time; but when poll jitter shifts them later, the topology relation
# still holds.
IsLayer(a1, OTS) ^ IsLayer(a2, OTU) ^ OnSamePort(a1, a2)
  ^ Earlier(a2, a1)        # observed time may have a2 first
  ^ WithinSix(a1, a2)
   => RootCandidate(a1) ^ Secondary(a2, a1)
```

```
# Equipment / environment alarms can be late even though they ARE the
# root.  TEMP_OVER alarms at +120s after a FAN_FAIL are still secondary;
# but a FAN_FAIL observed AFTER a TEMP_OVER (because the FAN_FAIL poll
# was slower) is still the root.
Name(a1, FAN_FAIL) ^ Name(a2, TEMP_OVER) ^ OnSameNE(a1, a2)
  ^ WithinSix(a1, a2)
   => RootCandidate(a1) ^ Secondary(a2, a1)
```

**Bottom line**: any rule that uses `Earlier(...)` for root inference is suspect. Prefer rules that use `Close` / `WithinSix` (window membership) plus topology / layer.

---

## 7. Cross-service correlation (R4, grounded)

R4 says two OCh alarms whose paths share a span are correlated. Specifically for the v3 lab mesh, the only span that two services share is `span_OMS3_BC` (carried by `ch2` and `ch4`). The rule that turns R4 from correlation into root-cause inference:

```
# A single shared-span fault explains BOTH services' OCh-layer alarms.
IsLayer(a1, OCH) ^ Container(a1, och1) ^ AtNE(a1, n1)
  ^ IsLayer(a2, OCH) ^ Container(a2, och2) ^ AtNE(a2, n2)
  ^ Traverses(och1, span) ^ Traverses(och2, span)
  ^ Close(a1, a2)
  ^ Exists r: Name(r, OTS_LOS) ^ Container(r, span)
   => Secondary(a1, r) ^ Secondary(a2, r)
```

If the OTS_LOS at `span` is in the alarm set, all OCh alarms on tunnels traversing it are secondary to it. If it's *not* present (suppression cleared it, or polling delay hasn't surfaced it yet), the OCh alarms are still siblings:

```
# Sibling rule (no shared root yet): two OCh alarms on the same shared span
# are correlated even without the explicit OTS_LOS.
IsLayer(a1, OCH) ^ IsLayer(a2, OCH)
  ^ Traverses(Container(a1), span) ^ Traverses(Container(a2), span)
  ^ Close(a1, a2)
   => Correlated(a1, a2)  [w=0.3]   # = R4
```

---

## 8. Fault-class fingerprint rules

Each of the 10 ★ faults the simulator implements has a characteristic alarm constellation. These rules go from "we know which alarms are present" to "the root is fault X". They also serve as features for an ML classifier predicting `incidents.fault_type`.

### 8.1 fiber_cut

```
RootCause(r, "fiber_cut") <=
   Name(r, OTS_LOS) ^ Container(r, span) ^ IsSpan(span)
   ^ Exists a: Name(a, MUT_LOS) ^ Container(a, span) ^ Close(r, a)
```

### 8.2 fiber_attenuation_high

```
RootCause(r, "fiber_attenuation_high") <=
   Name(r, IN_PWR_LOW) ^ Container(r, span) ^ IsSpan(span)
   ^ Exists a: Name(a, BEFFEC_EXC) ^ Carries(Container(a), span) ^ Close(r, a)
   ^ Exists a': Name(a', OTU_DEG) ^ Carries(Container(a'), span) ^ Close(r, a')
   ^ NOT Exists o: Name(o, OTS_LOS) ^ Container(o, span)
```

(The negation is what distinguishes a *partial* fiber loss from a full cut.)

### 8.3 b1_exc_high / b2_exc_high / fec_pre_cor_high

```
RootCause(r, "b1_exc_high") <=
   Name(r, B1_EXC) ^ IsLayer(r, SDH)
   ^ NOT Exists o: Name(o, OTS_LOS) ^ OnSameSpan(r, o)

RootCause(r, "b2_exc_high") <=
   Name(r, B2_EXC) ^ IsLayer(r, SDH)
   ^ NOT Exists o: Name(o, OTS_LOS) ^ OnSameSpan(r, o)

RootCause(r, "fec_pre_cor_high") <=
   Name(r, BEFFEC_EXC) ^ IsLayer(r, OTU)
   ^ Exists a: Name(a, FEC_BEF_COR_ER) ^ OnSamePort(r, a)
   ^ NOT Exists p: Name(p, IN_PWR_LOW) ^ OnSameContainer(r, p)
```

### 8.4 rx_power_drift

```
RootCause(r, "rx_power_drift") <=
   (Name(r, IN_PWR_LOW) v Name(r, IN_PWR_HIGH)) ^ IsLayer(r, OTS)
   ^ Exists a: Name(a, IN_PWR_ABN) ^ OnSamePort(r, a)
   ^ NOT Exists o: Name(o, OTS_LOS) ^ OnSameSpan(r, o)
```

### 8.5 ne_power_off

```
RootCause(r, "ne_power_off") <=
   Name(r, POWER_ABNORMAL) ^ AtNE(r, n)
   ^ ForAll s: (IsSpan(s) ^ source(s, n)) =>
       Exists o: Name(o, OTS_LOS) ^ Container(o, s) ^ Close(r, o)
```

### 8.6 board_power_off

```
RootCause(r, "board_power_off") <=
   Name(r, BD_OFFLINE) ^ AtBoard(r, b)
   ^ Exists a: Name(a, R_LOS) ^ AtPort(a, p) ^ PeerPort(p, q)
       ^ Exists tp: tp.uuid = q ^ tp.board = b ^ Close(r, a)
```

### 8.7 fan_failure

```
RootCause(r, "fan_failure") <=
   Name(r, FAN_FAIL) ^ AtBoard(r, b) ^ b.type = "FAN"
   ^ Exists a: Name(a, TEMP_OVER) ^ OnSameNE(r, a) ^ a.board != b
```

### 8.8 config_mismatch

```
RootCause(r, "config_mismatch") <=
   (Name(r, OPU_PLM) v Name(r, OTU_TIM) v Name(r, ODU_PM_TIM))
   ^ Exists a, a': three of {OPU_PLM, OTU_TIM, ODU_PM_TIM} co-occur on the same port
       ^ OnSamePort(r, a) ^ OnSamePort(r, a')
   ^ NOT Exists l: Name(l, OTS_LOS) ^ OnSameNE(r, l)
```

---

## 9. Compound (multi-root) disentanglement rules

When `group_id` is unknown at inference time (we observe the cascade but don't know how many roots fired), we need rules that segment the alarm set into per-root groups.

### 9.1 Spatial separation

```
# Two candidate roots whose topology footprints don't overlap are
# independent incidents — every alarm in each footprint belongs to its
# corresponding root.
RootCandidate(r1) ^ RootCandidate(r2)
  ^ NOT SharedTopology(r1, r2)
  ^ Reaches(r1, a)
   => Group(a) = Group(r1) ^ Secondary(a, r1)
```

`SharedTopology(r1, r2)` is true when `r1` and `r2`'s "downstream cones" (the set of NEs / spans / tunnels their cascades can reach via the propagation rules above) intersect. `Reaches(r, a)` is the transitive closure of the propagation rules from `r` that can produce `a`.

### 9.2 Closest-root assignment

```
# When two candidate roots share some downstream topology, each
# secondary alarm is assigned to whichever root is closer (fewer
# topology hops; ties broken by smaller |T_obs(r) - T_obs(a)|).
Secondary(a, r1) ^ Secondary(a, r2)
  ^ TopoDistance(a, r1) < TopoDistance(a, r2)
   => Group(a) = Group(r1)
```

### 9.3 Layer-anchored assignment

```
# An ODU client-TP alarm belongs to the root that affects its underlay
# OCh.  This resolves multi-root ambiguity even when topology distance
# is identical.
Name(a, ODU_PM_SSF) ^ Container(a, odu) ^ RidesOn(odu, och)
  ^ Traverses(och, span1) ^ Traverses(och, span2)
  ^ Container(r1, span1) ^ Container(r2, span2)
  ^ RootCandidate(r1) ^ RootCandidate(r2)
   => Group(a) = ChooseByOSL(r1, r2)
```

(`ChooseByOSL` = pick whichever root is at the "Optical Section closest to the ODU's destination" — typically the receive end of the path.)

---

## 10. Confidence & rule combination

Three classes of rule, three combination policies:

| Class             | Rules                              | Combination                                            |
|---|---|---|
| **Deterministic** | layer cascade (§3), peer-port (§4), equipment trump (§5) | If any deterministic rule fires for a pair, it overrides every weighted rule. |
| **Weighted**      | R1–R4 (§2)                          | Sum of weights of all rules that fire on a pair (max = 0.1+0.2+0.4+0.3 = 1.0). Pair-level threshold ≥ 0.5 ⇒ `SameIncident`. |
| **Fingerprint**   | §8 fault-class rules                | At most one fingerprint per incident; the fingerprint that matches all of its premises wins. If two match, prefer the more specific one (more positive premises, more `NOT Exists` clauses). |

For the ML pipeline:
- **Stage 1 (correlation)**: weighted R1–R4 + temporal grouping → cluster alarms into incidents.
- **Stage 2 (root-cause picking)**: layer + topology rules pick one alarm per cluster as `RootCandidate`; if multiple candidates remain, fingerprint rules (§8) tie-break.
- **Stage 3 (compound check)**: §9 splits any cluster whose internal alarms can't be explained by a single root.

---

## 11. Implementation hints

### 11.1 SQL-only baseline

Many rules become one query. The propagation-edge query that builds the ~5000-edge propagation graph from the dataset:

```sql
SELECT root.alarm_name, der.alarm_name, count(*)
FROM alarms root
JOIN alarms der ON der.incident_id = root.incident_id AND der.id != root.id
JOIN incidents s ON s.id = root.incident_id
WHERE root.is_root = 1
  AND root.group_id = der.group_id
  AND s.suppressed = 0
  AND ABS(root.t_observed_ms - der.t_observed_ms) <= 5*60*1000
GROUP BY root.alarm_name, der.alarm_name;
```

The `ABS(...) <= 5*60*1000` clause is rule R1 in SQL. Adding the layer/container conditions reproduces R2–R4 plus the deterministic cascade rules from §3–§5.

### 11.2 Datalog / Prolog evaluation

Predicates 1.1–1.4 map directly to facts; rules 2–9 to clauses. A small Datalog engine (`pyDatalog`, `clingo`, or a hand-coded fixed-point loop) evaluates the inference goal `RootCause(?, X)` over the asserted facts. Each `incidents.db` row → ~100 facts; the inference cost is dominated by §2's transitive closure.

### 11.3 GNN supervision

Treat each alarm as a node; edges:
- existing in a **propagation-graph candidate** (mined from §5.4 query in `docs/02_simulator_user_guide.md`)
- annotated with which rule(s) fire — a 12-d feature per edge (1 bit per rule §3.x, §4.x, §5.x, §6.x, §7).

Train the GNN to predict `is_root` and `group_id` per node. The rules above constrain the training signal: positive `Secondary(a, r)` edges from rules 3–5 are guaranteed positives; R4 sibling pairs are guaranteed `SameIncident` (no direction).

### 11.4 Rule-engine first, model second

For an initial RCA system, evaluate all deterministic rules (§3–§5, §8) in Python against an incoming alarm set. They cover the easy 80 % of cases. Use the ML model only for the residual cases — pairs where weighted R1–R4 fire but no deterministic rule does. This is the architecture sketched in `Library/Picture1.png`: alarm correlation group → propagation-relation reasoning → supplementary-rule reasoning → root-cause alarm.

---

## 12. Validation against the simulator

Every deterministic rule in §3–§9 has a corresponding SQL spot-check that should return ≥ 99 % of expected co-occurrences in `incidents.db`. The five most useful checks:

```sql
-- §3: OTS_LOS at port suppresses every other alarm at the same port
SELECT count(*) FROM alarms a1 JOIN alarms a2
  ON a1.incident_id=a2.incident_id AND a1.port=a2.port AND a1.id!=a2.id
WHERE a1.alarm_name='OTS_LOS' AND a1.is_root=1 AND a2.is_root=0;

-- §4 peer-port: every fiber_cut incident has both an OTS_LOS and an R_LOS
SELECT count(*) FROM incidents s
WHERE s.fault_type='fiber_cut'
  AND EXISTS(SELECT 1 FROM alarms WHERE incident_id=s.id AND alarm_name='OTS_LOS')
  AND EXISTS(SELECT 1 FROM alarms WHERE incident_id=s.id AND alarm_name='R_LOS');

-- §5: every fan_failure has FAN_FAIL as root and TEMP_OVER as derived
SELECT count(*) FROM incidents s
WHERE s.fault_type='fan_failure'
  AND EXISTS(SELECT 1 FROM alarms WHERE incident_id=s.id AND alarm_name='FAN_FAIL' AND is_root=1)
  AND EXISTS(SELECT 1 FROM alarms WHERE incident_id=s.id AND alarm_name='TEMP_OVER' AND is_root=0);

-- §7 cross-service: every fiber_cut on span_OMS3_BC affects ch2 AND ch4
SELECT count(*) FROM incidents s
WHERE s.fault_type='fiber_cut' AND s.fault_target='span_OMS3_BC'
  AND EXISTS(SELECT 1 FROM alarms WHERE incident_id=s.id AND container_id='odu_ch2')
  AND EXISTS(SELECT 1 FROM alarms WHERE incident_id=s.id AND container_id='odu_ch4');

-- §6 polling-delay reality check: how often is the root NOT the earliest observed?
SELECT count(*) FROM incidents s
WHERE (SELECT alarm_name FROM alarms WHERE incident_id=s.id ORDER BY t_observed_ms LIMIT 1)
   != (SELECT alarm_name FROM alarms WHERE incident_id=s.id AND is_root=1 LIMIT 1);
-- Currently 20640/25600 = 80.6%
```

Use these as smoke tests when refining the rule set. A reasoner that ignores §3–§5 will get the easy cases wrong; a reasoner that ignores §6 will look right on `t_offset_ms` but wrong on `t_observed_ms` (production data).

---

## 13. What's not yet codified

- **Concurrent independent faults** — §9 handles 2–3 roots; > 3 needs propagation-graph clustering, not pure rules.
- **Soft → hard transitions** — `fiber_attenuation_high` (soft loss) escalating to `fiber_cut` (full LOS) should be one incident, not two. Needs a temporal ordering rule keyed on root-name pairs.
- **Protection switching** (`APS_*`, `OLP_*`) — v3 doesn't simulate it; rules will be added when v4's `protection_switch_failure` fault is implemented.
- **Cross-NE clock-sync cascades** — `SYNC_C_LOS` propagates through the SyncE chain. The simulator emits the alarm but no rule yet captures the per-NE chain ordering.

These gaps are the v2 backlog for this rule set.
