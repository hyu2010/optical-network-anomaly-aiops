# 04 — The Decision Procedure: Building the Diagnosis Tree

`diag_agent` builds a **diagnosis tree** for each incident. The tree is the decision procedure that leads from the symptoms to a root cause, and it is shown to the user while it grows.

All reasoning happens in working memory, through the steps below.

| Step | What it does | Section |
|---|---|---|
| Frame | Build the case frame deterministically from the observations | §3 |
| Generate | Propose hypotheses from several independent lines. The LLM line is guardrailed by an **agent-led KG walk**. | §4, §5 |
| Pool | Mix the lines into a prior with an explicit "none of these" (NOTA) | §4.5 |
| Winnow | Weigh each hypothesis with likelihood terms from documented effects, graph consistency, the PPE twin and checks. A predicted absence counts only after its observation window has closed. | §6 |
| Analogy | Compare the current role signature with past episodes from episodic memory | §10 |
| Bayesian synthesis | Combine tempered log-likelihood terms in a ledger; commit, report a set, or say "I don't know" | §7 |
| Plan | Choose the next check by expected information per hour | §8 |
| Tree | Record every step as tree nodes. Questions are ordered by information gain. Render as ASCII, Mermaid or JSON. | §9 |

All numbers in the examples come from the fibre-cut test case (doc 07), which was computed with these exact rules.

## 1. The diagnosis tree

### 1.1 Node model

The tree is stored in `working_memory.tree` and mirrored at closure as `dx:DiagnosisTree` / `dx:TreeNode` in RDF.

```python
class TreeNode(BaseModel):
    id: str                      # "n07"
    parent: str | None
    kind: Literal["frame", "generate", "hypothesis", "analogy", "evaluate", "question",
                  "check", "answer", "synthesis", "decision", "verify", "pruned"]
    cycle: int                   # decision-cycle number (0 = framing)
    t_agent: float               # agent clock, seconds after the onset reference
    label: str                   # short display text
    detail: dict                 # kind-specific payload (below)
    mass: float | None           # probability mass reaching this node (question/answer branches)
    posterior: dict[str, float] | None   # ledger snapshot after this node, by hypothesis id
    taken: bool = True           # False = forward-planned branch not (yet) executed
    evidence: list[str] = []     # obs_log ids read by this node
```

| Kind | Meaning | `detail` |
|---|---|---|
| `frame` | Case frame built | onset interval, scope size, `S#`, `V#`, `E#`, quiet instruments |
| `generate` | One generation line ran | line, weight, distribution, residual, walk provenance |
| `hypothesis` | A pooled candidate `H#` | mode, location, prior, line support |
| `pruned` | A candidate dropped before pooling, or a rejected LLM proposal | reason (e.g. "reverse-fibre rx quiet", "undocumented mode WSS_PORT_FAIL") |
| `analogy` | Episodes retrieved | top episodes, similarities, verification weights |
| `evaluate` | One cycle of winnowing | per-hypothesis observation-window states and the terms they feed |
| `question` | A test that partitions the hypotheses. It is either *already answered* by observations, or a *check* to be run. | question text, answer classes and the hypotheses in each |
| `check` | A check chosen by the advisor | check id, target, bits, effort, rate, rationale |
| `answer` | One answer class of a question or check | answer, hypotheses kept, mass |
| `synthesis` | Ledger update | terms, tau, NOTA, entropy |
| `decision` | Commit, set, IDK, regenerate or limit | rule results |
| `verify` | Post-commit check of closed windows or repair | updated posterior |

### 1.2 Two views of the same tree

The same nodes support two views:

- **The decision view (default).** The procedure as a decision tree.
  - Internal nodes are *questions*: an observation partition or a check.
  - Edges are answer classes, labelled with their probability mass.
  - Leaves are decisions.
  - The branch actually taken is solid. Forward-planned alternatives are dashed (`taken = False`) and show what the agent *would* do for each other answer.
  - This is the tree an operator can reuse as a procedure, and it is the candidate `dx:Procedure` at learning time.
- **The trace view.** The chronological reasoning trace: frame → generation lines → pooled hypotheses → evaluation → synthesis → check → answer → synthesis → decision → verify. It shows *how* the agent reasoned, including pruned candidates and rejected LLM proposals.

## 2. The decision cycle

```
frame ← build_frame(observations ≤ clock)                              # §3
H, prior ← pool(generation_lines(frame))                               # §4–5
loop:
    preds ← propagate(H)                                               # §6.1
    states ← window_states(preds, observations ≤ clock, clock)         # §6.2 observation windows
    terms ← evidence_terms(H, states, checks_done)                     # §6.3 winnowing terms
    post ← ledger(prior, terms)                                        # §7 the ledger, which alone commits
    tree += questions(evidence partitions), synthesis(post)            # §9
    match decide(post, states, budget):                                # §7.3
        COMMIT          → emit; verify while windows close; break
        REGENERATE      → widen ×2/×4/×8; H, prior ← re-pool; continue   (≤ 3)
        IDK / LIMIT     → report the 90 % set (flag "probably not considered"); break
        CONTINUE        → a ← advisor(H, post, preds, budget)          # §8
                          tree += check(a), forward branches
                          clock, observations ← execute(a)              # grounding
```

## 3. The case frame (deterministic)

The frame is computed by code from the substrate only, and no LLM ever summarises it. Building the frame with an LLM cost 22–24 points of top-1 accuracy on the Janz test bed.

### 3.1 Ingest and grouping

1. **Read the observations.** Alarms are read with `t_observed ≤ clock`, using the raw view by default, or the suppressed view with `--view suppressed`.
2. **Interpret each alarm type.** Each type is mapped to a **Condition** through `optx:indicatesCondition`, and its layer is taken from `optx:alarmLayer`.
3. **Group alarms into frame items.** A frame item is a (Condition, resource) pair. Same-location suppression children (`optx:suppresses`, e.g. `MUT_LOS` under `OTS_LOS`) fold into their parent's item. This keeps one physical observation from being counted several times.
4. **Number the items.** Each item becomes a symptom `S#`, in order of observation time.

### 3.2 Onset interval

A root-capable observation at time `t_obs`, on a layer with polling cadence `c`, places the onset in `[t_obs − c, t_obs]`. The onset interval is the intersection of these intervals over the earliest items of each layer, using physical delays taken from the alarm type.

For the fibre cut:

| Observation | Time | Cadence | Onset interval |
|---|---|---|---|
| OTS_LOS | +3.1 s | 5 s | [−1.9, +3.1] |
| MUT_LOS | +4.4 s | 5 s | [−0.6, +4.4] |
| **Intersection** | | | **[−0.6 s, +3.1 s]** |

The onset interval anchors three time windows:

| Window | Span | Purpose |
|---|---|---|
| `W_pre` | [t₀ − 480 s, t₀ − 20 s] | Precursors and recent changes (context) |
| `W_onset` | [t₀ − 20 s, t₀ + 120 s] | Symptoms |
| `W_post` | from t₀ + 150 s | Clears, verification and late layers |

**Framing hold.** Cycle 1 starts 32 s after the first alarm is ingested, which is the OTU-layer cadence (30 s) plus 2 s slack. By then the OTS, OCh and OTU windows of a hard-failure cascade have closed. The hold is configurable (`frame.hold_s`).

### 3.3 Scope, services, shared elements and quiet instruments

- **Scope.** The resources within two KG hops of any symptom, plus the `dx:dependsOn` closure of every affected service.
- **Services `V#`.** Client services whose ODU carries an `ODU_PM_SSF` (down) or a degrade item (degraded), together with the services that are still up in scope.
- **Shared elements `E#`.** Resources on the paths of two or more affected services, from the `optx:traverses` intersection. Examples: E1 `span_OMS3_BC`, E2 the `OMS3` section, E3 the drop board `C.OTU`.
- **Quiet instruments.** Instruments in scope that observed *nothing*. For each one the frame records the layer and the time its window closes. In the fibre cut, these include:
  - B's receive side of `span_OMS3_CB`;
  - `B.OTU.line_rx_C`;
  - A's receive side of `span_OMS2_BA`, and D's of `span_OMS5_BD`;
  - the drops of ch1 and ch3;
  - B's equipment, which stays *pending* until +125.1 s.
- **Context.** Maintenance windows, recent topology changes (`g:net` validity) and open incidents on overlapping scope.

### 3.4 Instance form and role form

The frame exists in two forms:

- **The instance form** names resources, e.g. `S1 = LossOfSignal@C.FIU.line_from_B [OTS]`.
- **The role form** replaces resources with their relation to the anchor and to each other, e.g. `S1 = LossOfSignal [OTS] at rx_of_span(E1)`, `V1, V2 share span E1`.

Generation and evaluation use the instance form. Analogy retrieval uses the role form, so a cut on OMS6 can recall one on OMS3.

## 4. Hypothesis generation: independent lines

Each line returns a distribution over `(mode, location)` hypotheses plus a **residual**, which is its own "none of these" mass. A line that has nothing to say **abstains**: its weight is removed rather than spread.

| Line | Weight | How it proposes | Residual |
|---|---|---|---|
| **Abduction walk** (§4.1) | 1.0 | Backward walk from each symptom over location roles, then forward propagation and scoring | 0.05 + 0.4 × unexplained share, at most 0.5 |
| **Set cover** (§4.2) | 1.0 | The smallest set of causes (≤ 3) from the walk's top 40 that covers 95% of items | 1 − covered share |
| **Personalised PageRank** (§4.3) | 1.0 | Random walk with restart on symptoms (damping 0.85), over the scope subgraph | 0.10 |
| **Alarm correlation** (§4.4) | 1.0 | Documented `dx:possibleCause` of the earliest-layer root-capable alarm, filtered by the frame | 0.10 (mass 0.9) |
| **Analogy** (§10) | 1.0 | Top 20 episodes; vote = similarity × verification weight | 0.05 + 0.5 × (1 − best similarity) |
| **Procedures** | 1.0 | A published `dx:Procedure` whose entry signature matches | 1 − match score |
| **LLM proposer** (§5) | 1.0 | KG-walk-guardrailed proposals of documented modes at location terms | stated NOTA + ungroundable mass |
| **Specialised lines** (monitoring, shared risk, trend) | 0.25 each | Narrow rules: shared-risk element 0.4/0.2, monitoring-fault patterns, PPE trend | 0 (narrow-line rule) |

The **standard** profile, used in the demo, runs four lines: walk, correlation, analogy and the LLM. The **full** profile adds the other lines. In **cold** mode, with no similar episodes, the analogy line abstains.

### 4.1 Abduction walk

1. For each symptom, the walk follows every effect role *in reverse*. The effect roles are the inverse of `dx:atRole` patterns; for example, `SPAN_RX_PORT⁻¹(C.FIU.line_from_B)` gives `span_OMS3_BC`.
2. This yields the candidate locations. At each location it keeps the documented modes, using the `dx:appliesTo` grain.
3. Each candidate is propagated forward (§6.1) and scored over the symptoms observed so far:

   | Outcome | Score |
   |---|---|
   | Each explained item | +1 |
   | Each missing Required effect whose window has closed | −1 |
   | Each missing Expected effect | −0.3 |

4. The 25 best candidates are kept, with probabilities proportional to their positive scores. Candidates whose score is zero or less become `pruned` nodes with a reason.

   | Pruned candidate | Reason |
   |---|---|
   | H6 `SECTION_CUT@OMS3` | Reverse-fibre receiver is quiet |
   | H7 `NE_POWER_LOSS@C` | C itself reported alarms |
   | H8 `CONNECTOR_DIRTY@span_OMS3_BC` | Explains no LOS item |

### 4.2–4.4 Set cover, PageRank and correlation

- **Set cover** handles compound incidents (§7.5).
- **PageRank** catches candidates the walk misses because their relations are incomplete, i.e. relations declared incomplete in `dx:CompletenessDeclaration`.
- **Correlation** takes the documented possible causes of the earliest root-capable alarm, after a quiet-instrument filter. For `OTS_LOS` these are FIBRE_CUT, SECTION_CUT, AMP_FAIL, BOARD_POWER_LOSS and NE_POWER_LOSS. Three of them survive, H1, H9 and H4, at 0.3 each.

### 4.5 Pooling

The pooled prior is built in four steps:

\[
\pi(h) = \frac{\sum_\ell w_\ell\, p_\ell(h)}{\sum_\ell w_\ell}, \qquad
\pi(\text{NOTA}) = \frac{\sum_\ell w_\ell\, r_\ell}{\sum_\ell w_\ell}
\]

1. **Weighted mixture.** Average the lines' distributions and residuals, as above.
2. **Floor.** Apply \( \pi \leftarrow 0.99\,\pi + 0.01/|H| \) on the named hypotheses, so no named hypothesis has zero prior.
3. **Abstain rule.** Lines that abstain are excluded from the weights.
4. **Narrow-line rule.** Specialised lines carry weight 0.25 and residual 0. They can reorder the named hypotheses but never move NOTA.

**Line weights.** The weights of 1.0 are the starting values. Once `diag-agent score` has measured each line's reliability on this test bed, the weights in `agent.line_weights` are set from those measurements. On the Janz test bed, equal pooling diluted a well-calibrated LLM line from 0.80–0.84 to about 0.50.

The fibre-cut prior is: H1 .4645, H9 .1749, H4 .1749, H3 .0512, H2 .0140, H5 .0091, NOTA .1114.

## 5. The LLM proposer, guardrailed by an agent-led KG walk

The LLM is one generation line. It is useful because it reads the frame the way an engineer does. It is also dangerous, because it names plausible but undocumented causes and invents locations. Three guardrails therefore apply:

- It may *walk* the graph only through bounded tools.
- It may *name* only documented fault modes.
- It may *place* a hypothesis only with a location term, anchored on a frame reference.

### 5.1 Tools (a ReAct subgraph, at most 8 tool calls)

| Tool | Returns | Guard |
|---|---|---|
| `frame_get()` | Deterministic rendering of the frame: `S#`, `V#`, `E#`, quiet instruments, onset, windows | Read-only; the LLM never builds the frame |
| `kg_neighbors(node, relation?, direction?)` | Up to 30 neighbours with their classes | Node must be in scope, or have been visited by an earlier call |
| `kg_term(term, anchor)` | Resources that a location term resolves to from `S#`, `V#` or `E#` | Term must be one of the 13 `dx:LocationTerm`s |
| `kg_modes(resource)` | Documented modes at that grain | — |
| `kg_effects(mode)` | Primary effects and roles, in words | — |
| `kg_preview(mode, term, anchor)` | Counts of items explained, missing (window closed) and pending | Deterministic propagator; no probabilities returned |

Every node a tool returns is added to `walk_visited`. The walk is agent-led: the LLM decides where to look next. The graph decides what exists there.

### 5.2 Output schema (structured output, validated by Pydantic)

```json
{
  "hypotheses": [
    {"mode": "FIBRE_CUT", "location": {"term": "upstream_span", "anchor": "S1"},
     "weight": 0.55, "walk": ["S1", "C.FIU.line_from_B", "span_OMS3_BC"],
     "rationale": "LOS at OTS on C rx of OMS3 with both ch2 and ch4 down; they share only span E1."},
    {"mode": "AMP_FAIL", "location": {"term": "upstream_amplifier", "anchor": "S1"},
     "weight": 0.20, "walk": ["S1", "span_OMS3_BC", "amp_B_OMS3"], "rationale": "…"},
    {"mode": "BOARD_POWER_LOSS", "location": {"term": "upstream_tx_unit", "anchor": "S1"}, "weight": 0.10, "walk": ["…"], "rationale": "…"},
    {"mode": "BOARD_POWER_LOSS", "location": {"term": "this_unit", "anchor": "S1"}, "weight": 0.05, "walk": ["…"], "rationale": "…"},
    {"mode": "WSS_PORT_FAIL", "location": {"term": "this_port", "anchor": "S1"}, "weight": 0.04, "walk": ["…"], "rationale": "…"}
  ],
  "nota": 0.06
}
```

### 5.3 Validation and location resolution

Each proposal goes through these checks in order. A failure sends the proposal's mass to the line residual and adds a `pruned` node with the reason.

1. **Documented mode.** The `mode` must be a `dx:FaultMode`. `WSS_PORT_FAIL` fails this check: it is undocumented, so its 0.04 goes to the residual.
2. **Known term and anchor.** The `term` must be one of the 13 location terms, and the `anchor` must exist in the frame.
3. **Location resolution.** `resolve_location(term, anchor)` must return at least one resource. If it returns several, the weight is split evenly between them.
4. **Grain.** The resolved resource's class must satisfy the mode's `dx:appliesTo`. There is no silent lifting from port to board.
5. **Walk provenance.** The resolved resource must be in `walk_visited`. This proves the LLM actually reached it rather than guessing.
6. **Normalisation.** Named mass, stated NOTA and the rejected mass sum to 1. Duplicate `(mode, location)` pairs are merged.

The LLM's weights are used only as a proposal distribution; on the Janz test bed its proposals were nearly calibrated. The LLM never sees the ledger and never sets a likelihood. If no LLM is configured (`llm.profile: none` or `FMA_NO_LLM=1`), the line abstains.

### 5.4 Prompt (procedural memory: `prompts/proposer.v1.md`, abridged)

> You are proposing candidate root causes for an optical-network incident. You are not deciding: winnowing and the ledger do that.
> Use the tools to walk the knowledge graph from the frame references. Name only fault modes returned by `kg_modes`.
> Place each hypothesis with one location term anchored on S#, V# or E#.
> Give weights that sum, together with `nota`, to 1. Reserve `nota` for causes you believe are not documented.
> Return JSON matching the schema.

## 6. Winnowing

Winnowing decides what is believed. Generation proposes candidates; winnowing weighs them. Each tool reads evidence the operator already holds, or the answer of a check, and supplies a likelihood term. The ledger (§7) is the only component that turns the prior and those terms into a posterior, and the only component that commits.

An observation window is the timing rule inside this step, not a name for it. A predicted effect is `PENDING` until the layer's poll cadence has elapsed, and a pending absence adds nothing. Once the window has closed, the same prediction is `MATCHED`, `MISSING` or `CONTRADICTED`, and the terms below may use it. `CHK_WAIT` and the checks in §8 are refined winnowing: further evidence when the terms already held do not separate the leaders.

### 6.1 Forward propagation

`propagate(h)` turns a hypothesis into predictions in three steps:

1. Resolve the mode's primary effects (`dx:atRole` walk patterns from the hypothesis location).
2. Close them under the cascade rules CR1–CR6 (doc 03 §4.2).
3. Add same-location suppression children and documented L2 associations as **Possible**.

Each prediction carries:

- the Condition, resource and layer;
- the strength;
- the delay `d`, summed along the cascade;
- whether the effect is lasting;
- an optional PPE signature.

### 6.2 Observation windows

A prediction on a layer with cadence `c` must arrive within

\[
W(p) = [\,t_0^{lo} + d,\; t_0^{hi} + d + c + 2\,\text{s}\,]
\]

| State | Condition | Effect on the term |
|---|---|---|
| `MATCHED` | An observation with the same Condition, or a subclass, on the predicted resource inside `W(p)` | Explains its item |
| `PENDING` | No match yet and the window is still open | **Nothing.** Absence is not evidence yet. |
| `MISSING` | Window closed, the instrument is alive, and nothing was seen | Debit by strength |
| `UNOBSERVABLE` | The instrument is predicted dead by this hypothesis, is not consulted (on-demand PPE), or is down | Declined; no debit |
| `CONTRADICTED` | An incompatible Condition was seen on the same resource and layer, e.g. LOS where low input power was predicted | Counted as missing, and the observed item stays unexplained |

Example windows for H1 at cycle 1 (t = +35.6 s):

| Prediction | Window (s) | State |
|---|---|---|
| OTS LOS at `C.FIU.line_from_B` | [−0.6, 10.1] | Matched at 3.1 |
| OCh LOS at `C.OTU.line_rx_B` | [−0.6, 15.1] | Matched at 7.9 |
| OTU SSF at `C.OTU.line_rx_B` | [−0.6, 35.1] | Matched at 18.6 |
| OTU BDI at `B.OTU.line_tx_C` | [−0.6, 35.1] | Matched at 26.0 |
| ODU SSF at `C.OTU.client_ch2` | [−0.6, 65.1] | Matched at 23.2 |
| ODU SSF at `C.OTU.client_ch4` | [−0.6, 65.1] | **Pending**; it arrives at 51.7 |
| PPE step in span for och_ch2 and och_ch4 | — | Unobservable (on-demand PPE not yet consulted) |

H9 (`AMP_FAIL`) has `EquipmentDown` at B.OA, with window [−0.6, 125.1]. It stays pending until +125.1 s, which is why H9 is still at 0 nats in cycle 1.

### 6.3 Winnowing terms

Each term is a log-likelihood in nats, the form every winnowing tool uses. Terms that read the **same evidence** share a tau budget of at most 1. The documented term and graph consistency judge equipment behaviour from the knowledge graph. The PPE term is this test bed's transmission twin: it compares the power-profile signature a cause would produce with the profile that was read. Checks are separate evidence items.

| Term | Reads | Tau | Rule |
|---|---|---|---|
| `T_doc`: the documented term | Alarm items | 0.7 | −2 per missing Required item, −0.6 per missing Expected item, −2 per unexplained item; capped at −8. NOTA is *charged* −2: an undocumented cause is assumed to fit at most as well as a documented mode two items off. |
| `T_gc`: graph consistency | Alarm items, KG | 0.3 | −4 per rule R1–R7 that holds, with at most 2 rules counted. NOTA is *neutral*. |
| `T_ppe`: PPE twin | PPE anomalies | 0.7 | The fitted PPE signature (kind, magnitude, position within `answerTolerance`) gives 0 if consistent and −8 if inconsistent. NOTA is charged −2. |
| `T_gc` (R7) | PPE anomaly, KG | 0.3 | The PPE location rule. NOTA is neutral. |
| `T_check:<id>` | The check's answer | Per the catalogue (`dx:tau`) | −8 for hypotheses whose predicted answer class differs; 0 otherwise |

**Neutrality.** A term that has nothing to say about NOTA gives it the *prior-weighted mean* of the named hypotheses' values:

\[
\ell_k(\text{NOTA}) = \frac{\sum_h \pi(h)\,\ell_k(h)}{\sum_h \pi(h)}
\]

In cycle 1, `T_gc` gives NOTA −1.4562, tempered to −0.4369. A neutral term thus neither rewards nor punishes "something else".

## 7. Bayesian synthesis: the ledger

### 7.1 Update

\[
\log P(h) = \log \pi(h) + \sum_k \tau_k \,\max\!\big(\ell_k(h),\; \max_{h'} \ell_k(h') - 8\big) - \log Z
\]

- **Clipping at 8 nats below the best** stops a single term from erasing a hypothesis whose fault is in the model rather than the network.
- **Tempering by tau** stops correlated terms from compounding.

The ledger is pure code, and every term is logged together with its inputs.

Cycle 1 of the fibre cut:

| | H1 | H9 | H4 | H3 | H2 | H5 | NOTA |
|---|---|---|---|---|---|---|---|
| Prior | .4645 | .1749 | .1749 | .0512 | .0140 | .0091 | .1114 |
| `T_doc` (tau .7) | 0 | 0 | −4 | −6 | −8 | −8 | −2 |
| `T_gc` (tau .3) | 0 | 0 | −4 (R1) | −8 (R1, R6) | −8 (R1, R3) | −8 (R1, R6) | −1.456 (neutral) |
| **Posterior** | **.70333** | **.26483** | .00485 | .000105 | .0000071 | .0000046 | **.02687** |

### 7.2 Commit and report rules

**Commit** when all of the following hold:

1. The leader's probability is at least 0.80.
2. NOTA is below 0.20.
3. Every term has been evaluated or declined for both the leader and the runner-up.
4. The **residual test** passes: no single frame item contributes more than 4.5 nats against the leader.

**Otherwise**, report the smallest set holding 90% of the mass. Flag it as "probably not considered" if NOTA is 0.20 or more.

### 7.3 Decide

| Decision | When |
|---|---|
| `COMMIT` | The commit rule holds. The agent then keeps verifying as pending windows close. |
| `REGENERATE` | NOTA is above 0.25, *or* entropy has fallen by less than 0.1 bit over the last 3 terms, *or* the residual test fails, which signals a misfit. The agent takes a return pass that widens the scope radius, the onset tolerance and the walk depth by ×2, ×4 and ×8 on passes 1–3. |
| `CONTINUE` | Otherwise; the advisor picks an action. |
| `IDK` | The limits are hit (12 checks, 8 h of effort, 3 return passes, or the live deadline). The agent reports the 90% set with an honest flag. |

The outcome is scored as one of:

- right, partial or wrong;
- IDK, justified (the cause was undocumented) or unjustified;
- non-convergence.

Hypotheses with identical predictions on every available instrument form an *equivalence class*. Committing to the class counts as right.

### 7.4 Return passes are visible

Every regeneration adds a `generate` node marked `pass k/3` and shows the number of returns left. A pass never removes earlier hypotheses; it only adds to them.

### 7.5 Compound incidents (peeling)

Compound hypotheses are generated in either of two cases:

- the leader explains less than 70% of the items (rule R3);
- the residual items form a coherent group, i.e. are connected in scope.

**How they are formed.** They come from set cover over the walk's top 40, with at most 3 members. Each compound's prior is \( \pi = \prod_i \pi(m_i) \times 0.5^{(n-1)} \).

**How they are evaluated.** Propagation takes the union of the members' predictions.

**How they are reported.** After commit, each member is reported as its own probable cause, and the incident's `probable-cause` list has more than one entry.

### 7.6 Future: learned specialists

The simulator's GNN (`ml/`) can later join as a further term. Its tau would be 0.3, sharing the alarm-evidence budget, which then becomes `T_doc` 0.5 + `T_gc` 0.2 + `T_gnn` 0.3. It would be validated by the same scoring harness, and it is never a generation line on its own.

## 8. Advisor, orchestrator and checks

### 8.1 Expected information per hour

For each admissible check `x`, the advisor groups the hypotheses **by predicted answer**, giving the answer classes \(a\). It then computes:

\[
\text{bits}(x) = H\big(P(a)\big) \;(\text{for deterministic answers; else the mutual information with tempered likelihoods}),
\qquad
\text{rate} = \frac{\text{bits}}{\text{effort}}
\]

where effort, in hours, is the check's minutes, plus 2 h for each site not yet visited, plus 1 h if the check interrupts service.

The advisor applies these rules:

- **"Can this decide?" gate.** A check of 0.5 h or more is admissible only if at least one of its answers, with probability of at least 5%, would lead to a commit.
- **Limits.** 12 checks, 8 h of effort, 3 returns.
- **Waiting is an action.** `CHK_WAIT` costs the time until the next pending window closes. Its bits come from the hypotheses that window separates.

Cycle 1 of the fibre cut:

| Check | Answer classes | Bits | Effort | Rate (bits/h) |
|---|---|---|---|---|
| `CHK_PPE_PROFILE(och_ch2)` | [a] step mid-span: H1 (.70333)<br>[b] step at span start: H9, H4, H5 (.26968)<br>[c] null: H3 (.0001055)<br>[d] no anomaly: NOTA, H2 (.02688) | 1.0086 | 1 min | **60.5** |
| `CHK_WAIT` (equipment window, +125.1 s) | equipment-down at B or C (.26979)<br>none (.73021) | 0.841 | 89.5 s | 33.8 |
| `CHK_ALARM_AUDIT(B)` | Same partition | 0.841 | 2 min | 25.2 |
| `CHK_OTDR(span_OMS3_BC)` | Break (H1)<br>no break | 0.877 | 45 min + 2 h site | 0.32 |
| `CHK_REVERSE_FIBRE` | All hypotheses predict "reverse fibre fine" | ≈ 0 | — | excluded |

### 8.2 Orchestrator (LLM planner, optional)

The orchestrator receives the top three admissible actions, with their answer classes, bits and effort, and the number of returns left. It may choose among them and must give a short rationale for an operator. Four rules constrain it:

- It cannot add an action.
- A request for an impossible action, such as "inspect the reverse fibre at B" when no check exists for it, is **refused visibly**: a `pruned` node records the reason.
- If it fails or is absent, the top-ranked action runs.
- The choice and the rationale are recorded on the `check` node.

### 8.3 Checks

There are 11 checks, held as `dx:Check` in the KB:

| Check | Kind | Minutes | Tau | Observes | Executor |
|---|---|---|---|---|---|
| `CHK_PPE_PROFILE` | Remote | 1 | 0.7 | PPE profile of one OCh: step, slope, null, rx_offset or clean, with position | Reads `ppe_anomalies` rows with `is_root = 0` and `ppe_traces` for that OCh, up to the clock |
| `CHK_ALARM_AUDIT` | Remote | 2 | 1.0 | Forces an immediate poll of an NE: equipment and environment alarms without waiting for the cadence | Substrate query without the polling delay for the audited NE. This is legitimate: it mirrors a real NMS resynchronisation. |
| `CHK_REVERSE_FIBRE` | Remote | 1 | 1.0 | Rx power or LOS on the reverse span | Substrate |
| `CHK_SERVICE_STATE` | Remote | 1 | 1.0 | Client service up or down | Substrate |
| `CHK_TRAIL_TRACE_AUDIT` | Remote | 3 | 1.0 | TTI or payload mismatch | Substrate |
| `CHK_WAIT` | Remote | 0 + wait | 1.0 | Closes pending windows | Advances the clock |
| `CHK_OTDR` | Field | 45 | 1.0 | Break or event position on a span | `FieldOracle` in replay; a human in live mode |
| `CHK_CONNECTOR_INSPECT` | Field | 20 | 1.0 | Dirty or damaged connector | Same |
| `CHK_BOARD_RESEAT` | Field, service-interrupting | 30 | 1.0 | Board recovers or not | Same |
| `CHK_LOOPBACK` | Field, service-interrupting | 15 | 1.0 | Fault inside or outside the loop | Same |
| `CHK_FAN_INSPECT` | Field | 10 | 1.0 | Fan state | Same |

**The `FieldOracle`.** In replay mode, field checks are answered by `FieldOracle`. Apart from the scorer, it is the only component permitted to read `fault_type` and `fault_target`, because it plays the engineer standing at the fibre. It answers only the question asked, with the check's tolerance, for example "break at 31 ± 1 km". Its answers enter `obs_log` like any other observation.

In live mode, a field check triggers a LangGraph `interrupt()`, and the run resumes when a human answers.

**PPE access mode.**

| Mode | Behaviour | Fibre-cut demo |
|---|---|---|
| `on_demand` (default) | PPE is read only through `CHK_PPE_PROFILE`, which shows the advisor at work | Commits at cycle 2 |
| `streamed` | PPE anomalies enter the frame as observations with a 30 s window | Commits at cycle 1 |

## 9. Tree building and display

### 9.1 How the tree grows

| Moment | Nodes added |
|---|---|
| Framing (cycle 0) | `frame` under the root, the incident question: "What is the root cause of incident 1001?" |
| Generation | One `generate` node per line. Below them, the pooled `hypothesis` nodes with priors, and `pruned` nodes. |
| Each evaluation | `evaluate` and `synthesis` nodes. Then the answered observation **questions**, built as described below. |
| Each plan | A `check` question node with one `answer` child per answer class, with mass. The advisor is run recursively, to depth 3, on the conditional posteriors of each answer whose mass is at least 1%, adding dashed forward branches. |
| Each decision | A `decision` leaf, then `verify` nodes. |

The answered observation questions are built as follows:

1. The evaluation states are partitioned into *questions*. For example: "Is there LOS at the B receiver of span_OMS3_CB?" has the answer *no, window closed*, which separates H3 from the others.
2. The questions are **ordered by information gain over the current prior**, as in ID3. The tree therefore reads as an efficient procedure, not as the order in which alarms happened to arrive.

### 9.2 Rendering

All three renderers read the same `DiagnosisTree`:

- **ASCII** (`render/tree_ascii.py`, using `rich.tree`): `diag-agent tree 1001 --view decision|trace`, or live in the console as deltas arrive.
- **Mermaid** (`render/tree_mermaid.py`): a `flowchart TD`. Taken edges are solid (`-->`) and forward-planned edges dashed (`-.->`). Node shape encodes the kind: question `{…}`, check `[/…/]` and decision `([…])`.
- **JSON** (`render/tree_json.py`): the node list. It is also served at `GET /diag/v1/incidents/{no}/tree`, streamed as `tree.node` SSE events, and referenced from the incident payload in extended mode (doc 06).

Decision view of the fibre cut after commit (rendered by `tree_ascii`):

```
Incident 1001 — what is the root cause of LOS on span_OMS3_BC?   [H1 .4645 → .99287 COMMIT]
├── Q1 LOS/SSF at B.OTU.line_rx_C (window closed +35.1 s)?  ─ no ─▶ H4, H3 ≈ eliminated  (IG .77 bit)
├── Q2 Did a resource predicted dead report alarms?  ─ yes (C.FIU, B.OTU) ─▶ H3, H5 eliminated (IG .33 bit)
├── Q3 Low input power instead of LOS at S1?  ─ no ─▶ H2 eliminated                    (IG .11 bit)
├── Q4 [check] PPE profile of och_ch2: where is the step?            60.5 bits/h, 1 min
│   ├── [a] mid-span, −30 dB @ 31.4 km in span_OMS3_BC  (.703)  ✔ taken
│   │   └── ◆ COMMIT FIBRE_CUT @ span_OMS3_BC, 31.4 ± 0.4 km   P=.99287  NOTA=.0067
│   │       └── verify +125.1 s: no equipment alarm at B   P=.99831
│   ├── [b] at span start (.270) ┄▶ Q5 [wait] equipment alarm at B.OA or B.FIU? ┄▶ AMP_FAIL | BOARD_POWER_LOSS
│   ├── [c] null (.0001)         ┄▶ CHK_BOARD_RESEAT(C.FIU_B)
│   └── [d] no anomaly (.027)    ┄▶ return pass 1/3 (NOTA, SPAN_LOSS_INCREASE)
```

## 10. Analogy reasoning with episodic memory

- **Signature.** The role form of the frame (§3.4) is a multiset of (Condition, layer, role) items plus:
  - the quiet-instrument roles;
  - the service pattern, e.g. "2 down, sharing 1 span";
  - the PPE kind, if known;
  - the earliest-layer profile.
- **Similarity.** A weighted Jaccard index over the signature items. Item weights are the inverse document frequency of each item across episodes, so a rare item weighs more than a common one.
- **Vote.** Each of the top 20 episodes votes \( \text{sim} \times w_{verif} \), with \( w_{verif} \) equal to 1.0, 0.7, 0.3 or 0 (doc 02 §5). The vote goes to its *conclusion relation* resolved onto the current incident. For example, an episode concluding "FIBRE_CUT at `upstream_span(S1)`" maps to `span_OMS3_BC` now. A relation that does not resolve abstains.
- **Generation only.** Analogy is a generation line, not a ledger term. The same past experience is therefore not counted twice. It also matters most for what it brings into consideration. For example, a mode seen before after an earlier wrong commit is brought back even when the walk ranks it low.
- **Display.** An `analogy` node lists the top episodes with their similarities, verification weights and the conclusion each maps to. In the fibre cut, the best episode is a verified OMS6 cut with similarity 0.80, which gives the line a residual of 0.15.

## 11. Narration

The narrator LLM writes the operator summary from a fixed template of slots: the leader, its probability, NOTA, the decisive questions and the check results. A validator checks that every number and resource id in the text appears in the ledger or the tree. If the validation fails, the deterministic template text is used instead. The narration is attached to the incident (`description`, `repair-advices`) and to the tree root.

## 12. Guardrails summary

| Risk (from the Janz findings) | Guard in this design |
|---|---|
| LLM confident and wrong | The LLM never scores (§5); the ledger commits only under the commit rule (§7.2) |
| LLM invents a cause or location | Documented modes, location terms, walk provenance; ungroundable mass goes to NOTA (§5.3) |
| LLM-built frame | The frame is code only (§3) |
| Early absence treated as evidence | Windows by layer cadence (§6.2) |
| Double counting | Suppression folding (§3.1), tau budget (§6.3), analogy kept out of the ledger (§10) |
| Missing documented mode leads to a wrong commit | NOTA charge, residual test, regeneration, look-alike modes kept in the KB (doc 03 §4.1) |
| Self-confirmation | Unconfirmed episodes have weight 0.3 (§10) |
| Ground-truth leakage | Substrate allowlist; only the `FieldOracle` and the scorer read labels (§8.3, doc 01 §5.3) |
