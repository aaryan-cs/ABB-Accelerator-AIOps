# DESIGN — Stateful causal graph + case library + early-warning classifier

Companion to MASTER_PLAN.md (the *what/why*) and BUILD_GUIDE.md (the *do-this*).
This file is the *proposal* for the next engine evolution: make the causal graph **hold state**
instead of flickering, and add a **case library** + **classifier** so the engine recognizes and
forecasts faults from what it has learned.

Status: **largely IMPLEMENTED and cluster-verified** (BUILD_LOG LOG-059→063) — Part A edge memory
(workload-keyed, structural floor), Part B case-merge into families, plus deviation-gated detection
(the §4 "Case 0 deviation" idea) all shipped; the learned classifier/early-warning (Part C) and the
topology-generated hypothetical cases (§2.2) remain proposals. Open items in `REMAINING.md`. Grounds every change in the real code
(`correlation/engine/{pipeline,gate,ranking,lagcorr,detectors}.py`, `correlation/service.py`).

---

## 0. The problem (why edges flicker)

The engine runs continuously — `service.loop()` recomputes every `ENGINE_INTERVAL` (10s) and
overwrites the module-global `_graph`. But **each pass is stateless and memoryless**: `run_pass`
is a pure function of the current ring, and `_graph` is replaced wholesale every cycle. So an edge
exists only during the passes where, *at that instant*, an onset clears `|z|≥3`, the three-clause
gate accepts, and `active≥2`. Any pass that misses (storm aged out, transient gap, noisy victim
baseline, temporal coin-flip) replaces the good verdict with an empty graph.

Typical idle/sub-storm reply (the common case):

```json
{ "findings": [ ... onsets ... ], "edges": [], "root_cause_ranking": [], "blast_radius": [],
  "meta": { "pods": 13, "active": 2, "accepted_edges": 0 } }
```

The verdict from LOG-056 (`cooling-monitor → timescaledb`, threshold-free) only surfaces
occasionally. The engine is correct; it just doesn't **persist** what it has confirmed, and there
is no **baseline** to show the live graph *shifting away from*.

Design goals:

1. **Hold edges.** A confirmed correlation persists across the gap passes and decays smoothly
   instead of vanishing. When nothing is unusual, the steady-state coupling graph is maintained —
   it *is* the baseline case.
2. **Shift under load.** When resources go abnormal, edges sharpen and re-weight, driven directly
   by the live resource signals; the graph visibly morphs with the disturbance.
3. **Remember.** A library of encountered + hypothetical cases, and a classifier that matches the
   live state to a case early — forecasting the next victim before the cascade completes.

Hard constraint: **`run_pass` stays a pure function** (the 13 fixtures must remain untouched).
All new state lives in the service layer / new modules, exactly where `build_inputs` already lives.

---

## 1. Part A — Stateful causal graph (two layers + edge memory)

### 1.1 Two layers

**Structural layer (slow, always present).** Every pass, correlate *witness-coupled* pairs
(`Witness.shared_relation` ∪ `psi_copressure`) over a long window and EWMA the result into a
persistent graph. This is the *normal coupling map* — who co-moves when nothing is wrong. It is
held through calm by slow decay and is always rendered (faint). This is the baseline that "creates
a case": you can only show edges *shifting* if you've been holding the steady state to shift from.
Structural edges are **descriptive coupling, not causal blame** — they do not require the temporal
clause; they require only stable correlation + a physical witness.

**Incident layer (fast, directional, gated).** The existing event-centred `run_pass` causal edges
(`accept_edge`: stat + witness + temporal, with evidence IDs + direction + lag). They rise fast,
overlay *hot* on the structural backbone during a disturbance, and are what the root-cause ranking
and blast radius are computed from.

Served `_graph` = structural (held, faint) ⊕ incident (hot, directional), reconciled by edge memory.

### 1.2 EdgeMemory — the multi-window stability `gate.py` promised but never persisted

A new `correlation/engine/state.py` holding an `EdgeMemory` (and a `GraphState` wrapper). One
instance lives in `service.loop()`; each pass feeds it the pure `run_pass` output and it renders
the graph that gets stored in `_graph`.

Per directed edge key `k = (workload(src), workload(dst), signal)` track:
`conf` (0–1 confidence), latest `r` / `lag_s` / `evidence`, `hits`, `last_seen_ts`, `structural` flag.

Update rule each pass:

```
edge accepted this pass        : conf ← conf + α·(1 − conf)      # α≈0.4 → ~3 passes to lock in
endpoints present, edge absent : conf ← conf·(1 − β)             # β≈0.1 → smooth fade, not a zero
endpoints absent (no data)     : hold conf                       # never penalize for missing samples
```

Render with **hysteresis**: show when `conf ≥ τ_hi` (e.g. 0.6), hide when `conf < τ_lo` (e.g. 0.25).
Promote to `structural=True` (slower β) when an edge stays confirmed across long calm windows or is
witness-backed and frequently seen. This is exactly the "multi-window stability" the gate docstring
cites as the answer to *correlation ≠ causation* — now actually retained between passes.

Effect: a real `cooling-monitor → timescaledb` edge is held through the gap passes that currently
zero it, and decays only if it genuinely stops recurring. No flicker.

> **Implemented — A1 (LOG-059).** `state.py` keys `edge_memory` by `stable_workload(src, dst)` as
> above, and the update rule (grow / decay-when-present / hold-when-absent) ships as written.
> Rendering adds the missing half: held edges are mapped back to the **current pod** for `/graph`
> and **skipped when a participating workload has no live pod** — so a dead pod-generation can never
> be served as active. Stale edges are therefore handled by **render-skip-and-preserve**, not by
> decaying them: the row stays in the DB (for retraining) but is withheld from `/graph`.
> `SCHEMA_VERSION → l3-memory-v2` clears the old pod-hash rows once. **Not yet built:** the
> `structural` flag / always-present baseline layer (§1.1, A2) — so a witnessed edge still decays to
> invisibility on quiet (visible in the watch as `conf` falling below `τ_lo`). That, plus the
> direction-stability fix, is the next slice.

### 1.3 Resource-driven edge weight (the "edges shift, influenced by resources" part)

Per workload, compute an **activation** scalar from the live signal — normalized recent psi_io /
severity (reuse the `findings[*].severity` and the `hot` test already in `build_inputs`):

```
activation(p) = clip( recent_max_psi(p) / COPR_REF , floor , 1 )   # floor ≈ 0.15 keeps the backbone visible at idle
render_weight(edge) = conf · |r| · max(activation(src), activation(dst))
```

At idle, `activation` sits at the floor → the structural backbone shows faintly. When a pod's
resource signal spikes, `activation → 1` and every edge incident to it lights up and sharpens —
the graph **morphs with load** instead of blinking. The dashboard (P6) maps `render_weight` to edge
width/opacity, so "edges shift, directly influenced by resources" is literal and visible.

### 1.4 Where it slots in (no change to `run_pass`)

```
service.loop():
    window, events = fetch()
    vectors, witness, breach = build_inputs(window, events)      # unchanged
    out = run_pass(vectors, witness, breach, window=ANALYSIS_WINDOW)   # PURE, unchanged (fixtures safe)
    state.observe(out, vectors, witness, ts)                     # NEW: edge memory + structural EWMA + activation
    _graph = state.render()                                      # NEW: served graph = held + hot, with conf/weight/state tags
```

New rendered edge fields: `confidence`, `state` ∈ {steady, confirming, active, decaying},
`age_s`, `last_confirmed_s`, `render_weight`. `meta` gains `held_edges`, `structural_edges`.
API (`api/main.py /api/graph`) passes these through; the dashboard distinguishes steady backbone
from a live incident.

### 1.5 New env knobs (no rebuild to tune, like ANALYSIS_WINDOW)

`EDGE_ALPHA` (0.4), `EDGE_DECAY` (0.1), `EDGE_SHOW` (0.6), `EDGE_HIDE` (0.25),
`STRUCTURAL_WINDOW` (longer trailing window for the slow layer, e.g. full ring),
`ACTIVATION_FLOOR` (0.15), `COPR_REF` (psi level that counts as full activation).

---

## 2. Part B — Case library (institutional memory)

### 2.1 What a case is

A **case** is a canonical fingerprint of a contention pattern, in workload space (pods normalized
via `workload()` so it generalizes across replicaset/pod hashes):

```
case = {
  id, family_id,                    # identity by prototype similarity, NOT an exact hash (see §3.2);
                                    # family_id groups variants of one underlying type
  stressors:  {workload, ...},      # who pulled resources (root_cause_ranking + leading burst/leak/saturation pods)
  victims:    {workload, ...},      # who got constrained (blast_radius / downstream onsets)
  signal,                           # psi_io | psi_cpu | psi_mem | latency_p95 | ...
  witness_kind,                     # pvc | psi | ebpf  (the physical coupling)
  motif:      [(src→dst), ...],     # directed edge subgraph
  lag_structure,                    # typical lags per edge
  # learned metadata:
  scenario_label,                   # S1..S5 when known (ground truth from P7 ledger), else null
  occurrences, first_seen, last_seen,
  typical_lead_time_s,              # stressor onset → victim onset (for forecasting)
  remediation                       # the action that resolved it (feeds P5 remediation card)
}
```

Example: `({cooling-monitor}, {timescaledb}, psi_io, pvc, [coolmon→tsdb], ~30s)` ← this is the
LOG-056 verdict, captured as Case S1.

### 2.2 Two populations

**Encountered** — observed and confirmed. Built by promoting a *stable incident* from EdgeMemory
(a subgraph whose edges held `state=active` for ≥ N passes) into a case **via the §3.2 similarity
merge** — fold into the nearest prototype if `sim ≥ τ_merge`, else open a new case (variant if
`≥ τ_family`, else novel) — incrementing `occurrences` and refining the running-consensus prototype,
`typical_lead_time_s`, and `remediation` each recurrence. This is the memory the P5 narrator cites:
*"seen 7×; throttling plc-gateway resolved it each time."*

**Possible / hypothetical** — the strong idea: **generate cases from topology before they ever
happen.** For every shared-resource clique in `Witness.shared_relation` / `psi_copressure`,
enumerate stressor subsets → predicted victims (the rest of the clique):

```
clique {cooling-monitor, dcim-bridge, timescaledb, log-archiver} on one PVC →
   {cooling-monitor, dcim-bridge} stress → timescaledb constrained   = case X
   {cooling-monitor, timescaledb} stress → dcim-bridge constrained   = case Y
   ...
```

These are exactly the "A+B → C, A+C → B" cases you described — **derivable a priori from the disk/
node graph**. They seed the classifier so it can recognize a contention pattern the *first* time it
occurs (zero-shot), and they make the hypothesis space explicit and auditable.

### 2.3 Storage

Recommendation: **SQLite on a small engine PVC** (portable, survives restarts, trivial to ship in
the air-gap tarball). Tables: `cases`, `case_observations` (one row per live match, for the P7
accuracy ledger), `case_remediations`.
Optional upgrade / nice demo line: store the case timeline in the cluster's own **timescaledb** —
the factory's DB becomes the AIOps long-term memory (dogfooding). Keep SQLite as the default so the
engine has no hard dependency on a watched workload.

---

## 3. Part C — Classifier / early-warning

Start **deterministic and explainable** (on-brand for the "correlation ≠ causation" defense),
graduate to a learned model once labeled runs accumulate from the P7 rehearsal ledger.

### 3.1 Feature vector (per pass)

Per workload: recent severity, slope/trend, `classify()` class, psi level, onset rank.
Pairwise: witness kinds, early correlation. Assemble into a fixed-width vector keyed by workload
role (the roster is fixed at 15 pods, so the schema is stable).

### 3.2 Matcher — DECIDED: Route 1 (deterministic similarity), conservative

Resolves §6.4. The matcher is the case engine's *identity, novelty, and recognition* layer in one.
A learned model is deferred to a labeled-data stretch (end of this section); it never owns case
identity or novelty.

**Representation (the prerequisite — kills the case explosion).** The current `_promote_case` keys a
case by `case_id = sha1(stressors, victims, signal, witness_kind, motif)`. A hash has no notion of
"near", so one extra co-victim or one motif edge mints a *new* case — this is why `cases` climbs
(10 → 18 …) on repeats of one fault. Replace exact hashing *as identity* with a **structured
prototype in a metric space**, so "alteration" = "small distance":

- stressors / victims        → workload **sets**
- motif                      → directed **graph** (≤15 nodes)
- signal, witness_kind       → **categoricals** (type-defining; hard-matched, see below)
- lag_structure, lead_time   → **numerics**

(Exact hashing survives only as a fast-path for *byte-identical* fingerprints; it is no longer the
identity.)

**Similarity.**
```
sim(a,b) = w_v·Jaccard(victims) + w_s·Jaccard(stressors)
         + w_m·motifSim(edge-set Jaccard / graph-edit) + w_l·lagSim
   gated by:  signal_a == signal_b  AND  witness_kind_a == witness_kind_b   (else sim = 0)
weights v0:  w_v .35, w_m .35, w_s .20, w_l .10        # motif + victims carry the type
```
`signal` and `witness_kind` are *type-defining* — a different physical coupling (psi_io+pvc vs
psi_cpu+ebpf) is a different type, never a variant — so they **hard-gate** rather than blend.

**Write-path merge (replaces exact-hash promotion in `_promote_case`).**
```
fp = fingerprint(stable_subgraph)        # promoted only after EdgeMemory state=active ≥ N passes
near, s = argmax_case sim(fp, case.prototype)
if   s ≥ τ_merge :  fold into `near` → occurrences++, update running-consensus prototype, log obs(Δ)
elif s ≥ τ_family:  new case, family_id = near.family_id        # same type, recorded as a variant
else            :  new case, new family_id                      # genuinely novel
```
**Conservative thresholds (decision): τ_merge 0.85, τ_family 0.60.** Bias = keep distinct cases
distinct; fold only near-identical signatures. Accepted risk: some over-splitting *within* a family
— tolerated because split cases still share `family_id`, so the type is never lost, only its
sub-variants multiply. (Looser merging is a later tuning call, not a redesign.)

**Running-consensus prototype (type vs alteration, no trained model).** A case is not pinned to its
first instance. Per field, accumulate across folded observations:
- victim / motif edges → keep with an occurrence frequency; **core** = present in ≥ `p_core` (≈0.8)
  of observations, **periphery** = the rest.
- lead time / lags → running mean + variance.

The low-variance **core is the type** (`coolmon→tsdb`, always present); the high-variance
**periphery is the alteration** (dcim-bridge sometimes a co-victim). That split *is* the
type/variant boundary — learned from data, deterministically, with no model.

**Family taxonomy (two levels).** Cases link by `family_id` (agglomerative at τ_family):
- family = "shared-disk I/O contention"        (loose — the *type*)
- case   = "S1 with dcim-bridge co-victim"      (tight — the *variant*)

**Novelty for free.** `max sim < τ_family` ⇒ new family. A closed-set classifier cannot say "this is
new"; the matcher gets it as a side effect of the distance test.

**Read-path output (the "engine should say that too" requirement).** The matcher returns a register
plus the **diff** against the matched prototype — not just a label:
- recurrence : `Case S1 #12 (sim .94)`
- variant    : `Variant of S1 (sim .78): +dcim-bridge co-victim`   ← prototype↔live set/graph Δ
- novel      : `No case ≥ .60 — logged as Case 19 (family: new)`

plus `predicted_victims`, `eta_s`, `recommended_remediation` carried from the matched case's history.

**Threshold calibration (deterministic, no training).** After a labeled S1/S2/S3 run set, pick τ so
within-scenario instances merge and across-scenario stay separate (cluster purity / silhouette) —
fully reproducible to evaluate.

**Deferred — learned layer (v1, only after the P7 ledger has ≥ ~50 labeled incidents).** A shallow
LR / small decision tree may then (a) *name* a family as S1..S5 and (b) re-weight the similarity
features from labels. The matcher stays the fallback + explanation; **write-path identity and
novelty remain the matcher's, never the model's** (a discriminative classifier is closed-set and
cannot do either).

### 3.3 Early warning (the headline)

Run the matcher on the **rising edge**, before the cascade completes. When stressor pods begin to
activate and the partial signature matches a known/hypothetical case above a similarity threshold,
emit an *incipient* finding **before the victim's onset**:

```
incipient: case S1 (sim 0.86) — predicted victim timescaledb in ~30s, remediation: throttle plc-gateway
```

Integration points already in the code:
- **`blast_radius()`** supplies predicted victims + ETA from the live graph — the matched case
  supplies them from *history* even before live edges form. Use the case ETA to pre-fill blast
  radius during the incipient phase.
- **`forecast_to_limit()`** already does leak/saturation ETA — combine with case lead time for a
  blended forecast.
- This realizes the deck's "forecast OOM *before* it happens" beat (S5) and the < 15s detect target.

---

## 4. My additional ideas

- **Case 0 = the steady state.** Treat the structural baseline graph itself as a stored case;
  "deviation from Case 0" becomes a first-class trigger signal, independent of absolute thresholds.
- **Bayesian accrual.** Each recurrence sharpens both the case signature and its lead-time estimate
  (running mean + variance), so confidence and forecasts tighten with experience.
- **Case → remediation linkage.** Every case stores the action that resolved it, feeding P5's
  remediation card directly — the library becomes the source of "what to do," not just "what broke."
- **Salience decay, never delete.** Old cases fade in match priority but keep their occurrence count,
  so rare faults aren't forgotten — important for a demo that runs the same scenarios repeatedly.
- **Confidence-gated narration.** P5 only narrates incidents whose EdgeMemory confidence cleared
  `τ_hi` (you already gate at 0.5 in the agent design) — fewer false alarms, steadier demo.
- **Counterfactual UI.** Because hypothetical cases are pre-enumerated, the dashboard can show
  "predicted but not yet observed" contention paths as ghost edges — a striking, defensible visual.

---

## 5. Phased rollout (keep `run_pass` pure throughout)

| Step | Scope | Risk | Fixtures |
|---|---|---|---|
| A1 | `EdgeMemory` (conf + hysteresis + decay) in `state.py`; render held graph | low | untouched (service-layer only) |
| A2 | Structural layer (slow EWMA over witnessed pairs) + activation re-weighting | low | untouched |
| A3 | API + dashboard pass-through of `confidence`/`state`/`render_weight` | low | n/a |
| B1 | Structured fingerprint + SQLite store; **similarity merge** (τ_merge/τ_family) + `family_id` — replaces exact-hash promotion, kills the case explosion | med | untouched |
| B2 | Topology-generated hypothetical cases from `Witness` cliques | med | untouched |
| C1 | Prototype/similarity matcher (read-path) + variant/novel diff + incipient early-warning wired to blast_radius | med | untouched |
| C2 | Learned classifier once P7 ledger has labels; keep matcher as fallback | higher | new unit tests |

Validation discipline (unchanged from the project's norm): `run_pass` fixtures stay **13/13** by
construction (no edits to the pure pipeline); add fresh unit tests for `state.py` (decay/hysteresis
math) and the case matcher; rebuild `skn/correlation-engine:v0.1` + `k3s ctr images import` +
`kubectl rollout restart deploy/correlation-engine -n aiops` per the standard loop.

## 6. Open decisions (yours to make)

1. **Storage backend** — SQLite-on-PVC (default, recommended) vs timescaledb (dogfood, adds a
   dependency on a watched workload).
2. **Structural window** — full 15-min ring vs a longer dedicated baseline buffer.
3. **Where activation re-weighting renders** — engine-side (`render_weight` in `_graph`) vs
   dashboard-side (raw conf + raw psi, weight computed in the frontend). Engine-side keeps every
   client consistent; dashboard-side keeps the API leaner.
4. **Classifier line** — *DECIDED: Route 1 — deterministic similarity matcher, conservative
   thresholds (τ_merge 0.85, τ_family 0.60). Learned model deferred to a post-P7 stretch and never
   owns identity/novelty; see §3.2.*
