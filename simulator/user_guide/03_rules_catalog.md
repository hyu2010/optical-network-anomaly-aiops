# Rules Catalog

A flat inventory of every rule defined in `Design/08_root_cause_rules.md`, cross-referenced with its implementation in `src/rules/`.

- **Spec source**: `Design/08_root_cause_rules.md`
- **Code root**: `src/rules/`
- **Orchestrator**: `src/rules/reasoner.py::infer_root` — runs §8 fingerprints first, then §3 / §4 / §5 to label secondaries.

Legend for the **Status** column:
- ✅ implemented in `src/rules/`
- ⚪ defined in the design doc but no code yet
- 🔄 partially implemented (see notes)

---

## §2 Correlation rules (weighted)

These rules group alarms into the same incident but do not name a root. Each pair's confidence = sum of weights of every rule that fires on it. Pair-level threshold ≥ 0.5 ⇒ `SameIncident`. Max possible weight = 0.1 + 0.2 + 0.4 + 0.3 = 1.0.

| ID | Name | Weight | Conclusion | Status | Implementation |
|---|---|---:|---|---|---|
| R1 | Time clustering — `Close(a1,a2) ^ WithinSix(a1,a2)` | 0.1 | `Correlated(a1, a2)` | ⚪ | not implemented |
| R1+ | Transitive R1 — `Correlated(a1,a2) ^ Close(a2,a3) ^ WithinSix(a1,a3)` | 0.1 | `Correlated(a1, a3)` | ⚪ | not implemented |
| R2 | OTU container vs OCh tunnel via `Traverses(och, otu)` | 0.2 | `Correlated(a1, a2)` | ⚪ | not implemented |
| R3 | Two ODU alarms on segments of the same client service | 0.4 | `Correlated(a1, a2)` | ⚪ | not implemented (would need multi-segment client services, which v3 lacks) |
| R4 | Two OCh alarms whose paths share a span | 0.3 | `Correlated(a1, a2)` | ⚪ | not implemented |

---

## §3 Layer-cascade rules (within a port, deterministic)

Layer order (upstream → downstream): **OTS → OMS → OCH → OTU → SDH/FEC/ODU/OPU → client**.

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| §3.1 | OTS → OMS @ same port within 5 min | `Secondary(OMS, OTS)` | ✅ | `layer_cascade.py::apply` (pass 2, via `LAYER_ORDER`) |
| §3.2 | OMS → OCH @ same port within 5 min | `Secondary(OCH, OMS)` | ✅ | `layer_cascade.py::apply` |
| §3.3 | OCH → OTU @ same port within 5 min | `Secondary(OTU, OCH)` | ✅ | `layer_cascade.py::apply` |
| §3.4 | OTU → ODU @ same port within 5 min | `Secondary(ODU, OTU)` | ✅ | `layer_cascade.py::apply` |
| §3.5 | OTU → SDH @ same port within 5 min | `Secondary(SDH, OTU)` | ✅ | `layer_cascade.py::apply` |
| §3.6 | ODU → client @ same port within 5 min | `Secondary(client, ODU)` | ✅ | `layer_cascade.py::apply` |
| §3.7 | OTS_LOS dominates everything on the same port | `Secondary(*, OTS_LOS)` | ✅ | `layer_cascade.py::apply` (pass 1, `DOMINATING_NAMES`) — source tag `§3.dominates.OTS_LOS` |
| §3.8 | MUT_LOS dominates everything on the same port | `Secondary(*, MUT_LOS)` | ✅ | `layer_cascade.py::apply` (pass 1, `DOMINATING_NAMES`) — source tag `§3.dominates.MUT_LOS` |

Note: the code's `Close` window (5 min) is implicit — the per-incident alarm set is already within one cascade, so all alarms at a port are eligible. No `T_obs` filter is applied.

---

## §4 Topology-grounded cascade rules (cross-port / cross-span, deterministic)

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| §4.1 | Peer-port LOS — OTS_LOS @ one TP, R_LOS @ the other TP of the same span | `Secondary(R_LOS, OTS_LOS)` | ✅ | `topology_cascade.py::apply` — source tag `§4.peer_port`; peer map built via `_peer_port_map` |
| §4.2 | Supporting-link cascade — OTS_LOS @ span → every OTU-layer alarm on a pipe `Carries(otu, span)` | `Secondary(OTU/OCH, OTS_LOS)` | ✅ | `topology_cascade.py::apply` — source tag `§4.supporting_link`; uses `_otus_supported_by` |
| §4.3 | Per-tunnel propagation — ODU_PM_SSF on an ODU whose OCh `Traverses` the faulted span | `Secondary(ODU_PM_*, OTS_LOS)` | ✅ | `topology_cascade.py::apply` — source tag `§4.per_tunnel`; uses `_odus_traversing`. Code also accepts `ODU_PM_BDI` (extension over the doc) |

---

## §5 Equipment & environment "trump cards" (deterministic)

NE/board-level events override every alarm at the same location.

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| §5.1 | POWER_ABNORMAL @ NE n → every other alarm at n is secondary | `Secondary(*, POWER_ABNORMAL)` | ✅ | `equipment.py::apply` — source tag `§5.power_abnormal` |
| §5.2 | BD_OFFLINE @ board b → every other alarm at b is secondary | `Secondary(*, BD_OFFLINE)` | ✅ | `equipment.py::apply` — source tag `§5.bd_offline` |
| §5.3 | FAN_FAIL @ NE n → TEMP_OVER @ n is secondary | `Secondary(TEMP_OVER, FAN_FAIL)` | ✅ | `equipment.py::apply` — source tag `§5.fan_fail` |
| §5.4 | HARD_BAD @ board b → every other alarm at b is secondary | `Secondary(*, HARD_BAD)` | ✅ | `equipment.py::apply` — source tag `§5.hard_bad` |

Important caveat (`reasoner.py` docstring): §5 alarms are **not** trusted as roots by name alone — L2 keyword expansion routinely pulls POWER_ABNORMAL into unrelated cascades. Root selection is done by §8 fingerprints, which carry the additional guards.

---

## §6 Polling-delay-aware rules

Address the v3 finding that 80.6% of incidents have the root alarm not as the earliest-observed alarm.

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| §6.1 | OTS @ port + OTU @ same port within 6 min, even if OTU arrives first in observed time | `RootCandidate(OTS) ^ Secondary(OTU, OTS)` | ⚪ | not implemented as a standalone rule. The behavior is implicitly produced by §3 (layer-cascade ignores `T_obs`) + §8 fingerprints (which use topology, not timestamps) |
| §6.2 | FAN_FAIL + TEMP_OVER @ same NE within 6 min, regardless of observation order | `RootCandidate(FAN_FAIL) ^ Secondary(TEMP_OVER, FAN_FAIL)` | ✅ | covered by `equipment.py::apply` (`§5.fan_fail`) for the Secondary half; the RootCandidate half is provided by `fingerprints.py::fp_fan_failure` (`§8.7`) |

---

## §7 Cross-service correlation (R4, grounded to root inference)

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| §7.1 | Two OCh tunnels share a span AND an OTS_LOS exists on that span | `Secondary(OCh1, r) ^ Secondary(OCh2, r)` where r = OTS_LOS | 🔄 | not a standalone §7 rule; the effect is produced by §4.2 (`§4.supporting_link`) firing once per affected OCh/OTU |
| §7.2 | Sibling rule — two OCh alarms share a span, no OTS_LOS present | `Correlated(a1, a2)` [w=0.3] | ⚪ | not implemented (no weighted correlation engine) |

---

## §8 Fault-class fingerprints (root pickers)

These are *the* rules that select the root alarm. The orchestrator picks the highest-weight match; ties broken by `FINGERPRINTS` list order (more specific first). All live in `fingerprints.py`.

| ID | Fault class | Required alarm + guard | Weight | Status | Implementation |
|---|---|---|---:|---|---|
| §8.1 | `fiber_cut` | OTS_LOS @ span + MUT_LOS on same span | 2.0 | ✅ | `fp_fiber_cut` |
| §8.2 | `fiber_attenuation_high` | IN_PWR_LOW @ span + BEFFEC_EXC on a topo container (span / OTU pipe) at the same NE, NO OTS_LOS on the span | 1.8 | ✅ | `fp_fiber_attenuation_high` (code drops the `OTU_DEG` requirement that the doc states, because the suppressed view eats it) |
| §8.3a | `b1_exc_high` | B1_EXC @ SDH layer, NO OTS_LOS on the same span, no stronger NE-level signature present | 1.6 | ✅ | `fp_b1_exc_high` → `_bit_error_fp`. Code adds NE-level blockers (`OTS_LOS`/`POWER_ABNORMAL`/`FAN_FAIL`/`OPU_PLM`) that the doc doesn't list |
| §8.3b | `b2_exc_high` | B2_EXC @ SDH layer, same guards as §8.3a | 1.6 | ✅ | `fp_b2_exc_high` → `_bit_error_fp`. Listed before B1 in `FINGERPRINTS` (B2 suppresses B1) |
| §8.3c | `fec_pre_cor_high` | BEFFEC_EXC @ OTU + FEC_BEF_COR_ER on same port, NO IN_PWR_LOW on container, no stronger NE-level signature | 1.7 | ✅ | `fp_fec_pre_cor_high` |
| §8.4 | `rx_power_drift` | IN_PWR_LOW (or IN_PWR_HIGH) @ OTS + IN_PWR_ABN on same port, NO OTS_LOS on span, NO BEFFEC_EXC on topo container at same NE | 1.7 | ✅ | `fp_rx_power_drift` (one candidate per port, prefers IN_PWR_LOW) |
| §8.5 | `ne_power_off` | POWER_ABNORMAL @ NE n + OTS_LOS on ≥60% of outgoing spans from n | 2.0 | ✅ | `fp_ne_power_off` (code loosens doc's `ForAll` to a 60% quorum to absorb `drop_prob=5%`) |
| §8.6 | `board_power_off` | BD_OFFLINE @ board b + peer-port R_LOS, OR BD_OFFLINE + no OTS_LOS at same NE (soft fallback) | 2.0 / 1.7 | ✅ | `fp_board_power_off`. Code adds the soft fallback (weight 1.7) for SCC/management boards that don't propagate to a peer |
| §8.7 | `fan_failure` | FAN_FAIL @ board b + TEMP_OVER on a different board at the same NE | 2.0 | ✅ | `fp_fan_failure` |
| §8.8 | `config_mismatch` | OPU_PLM @ port + all three of {OPU_PLM, OTU_TIM, ODU_PM_TIM} co-occur on the same port, NO OTS_LOS at the same NE | 1.8 | ✅ | `fp_config_mismatch` (only OPU_PLM is emitted as the candidate — the canonical root per `Design/05`) |

**Fingerprint priority order** (from `fingerprints.py::FINGERPRINTS`, applied on weight ties):
1. `ne_power_off` → 2. `board_power_off` → 3. `fan_failure` → 4. `fiber_cut` → 5. `config_mismatch` → 6. `fiber_attenuation_high` → 7. `fec_pre_cor_high` → 8. `rx_power_drift` → 9. `b2_exc_high` → 10. `b1_exc_high`.

---

## §9 Compound (multi-root) disentanglement rules

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| §9.1 | Spatial separation — non-overlapping downstream cones | `Group(a) = Group(r1)`, `Secondary(a, r1)` | ⚪ | not implemented |
| §9.2 | Closest-root assignment — by topology hops, ties by `|T_obs(r) - T_obs(a)|` | `Group(a) = Group(closer-root)` | ⚪ | not implemented |
| §9.3 | Layer-anchored assignment — ODU client TP → root that affects the underlay OCh closest to destination (`ChooseByOSL`) | `Group(a) = ChooseByOSL(r1, r2)` | ⚪ | not implemented |

`evaluate.py` notes that compound incidents (fault_type containing `+`) are scored leniently: a prediction is correct iff it matches any of the compound's roots — so §9 is deferred without breaking evaluation.

---

## Fallback rule (not in spec)

| ID | Rule | Conclusion | Status | Implementation |
|---|---|---|---|---|
| FB | No fingerprint matched → pick the alarm at the lowest layer (`OTS<OMS<OCH<OTU<SDH/ODU<client`) that nothing else labels Secondary; tie-break by `t_offset_ms` | `RootCause(fb)`, `fault_type=""` | ✅ | `reasoner.py::infer_root` — source tag `fallback.lowest_layer`. Design/08 §6 explicitly cautions this is unsafe under polling jitter; use only as graceful degradation |

---

## Summary

| Category | Rules in spec | Implemented | Notes |
|---|---:|---:|---|
| §2 Correlation (weighted) | 5 | 0 | No weighted-correlation engine yet |
| §3 Layer cascade | 8 | 8 | Full coverage |
| §4 Topology cascade | 3 | 3 | Full coverage; `ODU_PM_BDI` added beyond doc |
| §5 Equipment trump | 4 | 4 | Full coverage (but not trusted as roots — see §8) |
| §6 Polling-delay aware | 2 | 1 | §6.2 covered via §5.3 + §8.7; §6.1 implicit via §3 |
| §7 Cross-service (R4 grounded) | 2 | 0.5 | §7.1 effect produced by §4.2; §7.2 needs weighted engine |
| §8 Fault-class fingerprints | 10 | 10 | All ★ fault classes covered, with code-level guards beyond the doc |
| §9 Compound disentanglement | 3 | 0 | Deferred; `evaluate.py` scores compounds leniently |
| Fallback | — | 1 | `lowest_layer` heuristic, not in spec |
| **Total** | **37** | **27.5** | |

### Gaps tracked in `Design/08_root_cause_rules.md` §13

- Concurrent independent faults > 3 roots
- Soft → hard transitions (e.g. `fiber_attenuation_high` → `fiber_cut` as one incident)
- Protection switching (`APS_*`, `OLP_*`)
- Cross-NE clock-sync cascades (`SYNC_C_LOS` along SyncE chain)
