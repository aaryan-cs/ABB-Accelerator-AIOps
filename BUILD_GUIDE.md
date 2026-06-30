# BUILD GUIDE — SiliconKnights / ABB Accelerator Round 2

The hands-on companion to MASTER_PLAN.md. The plan says *what and why*; this guide says *do this, then check this*. When lost: find your symptom in the **Lost-finder** at the bottom, or re-read the phase's "Done when" box and work backwards.

**The three documents:**

| Doc | Role | When to open |
|---|---|---|
| MASTER_PLAN.md | architecture, decisions, competitive story | designing, writing the report, prepping answers |
| BUILD_GUIDE.md (this) | step-by-step build path P0→P8 | building, verifying, debugging |
| BUILD_LOG.md | append-only journal + decision register | start and end of *every* session |

**Working agreement (non-negotiable):**

1. Every session: read the last 3 LOG entries before touching anything; append ≥ 1 entry when you stop.
2. Every revert gets a `REVERT` entry linking what it undoes. Every ruling gets a `DECISION` with a D-number.
3. A phase is done only when every "Done when" box is checked — partial = `in progress`, say so in the log.
4. Anything that surprised you for > 30 min goes in the log as `BLOCKER`/`FIX` — that's the report's "challenges" section writing itself.

**Build order and dependencies:**

```
P0 env → P1 workloads → P2 telemetry → P3 aggregator → P4 engine → P5 language → P6 dashboard → P7 scenarios → P8 demo-hardening
                          (P1↔P2 can interleave)        (P6 backend can start after P3's schema freezes)
```

---

## P0 — Environment (Day 1)

**Goal:** a Linux node where eBPF, cgroup-v2 PSI, and K3s all demonstrably work.

**Status: DONE 2026-06-12 (LOG-020)** — desktop, Xubuntu 26.04, kernel 7.0.0-22-generic, all six checks green incl. PSI via cadvisor. WoL drill still pending (P0_DESKTOP_SETUP block 13).

**Steps:**

1. Pick the host (the stage demo uses either; **not WSL2** — D-003):
   - **Linux desktop, bare metal** (D-007 — the reference box): headless Ubuntu Server 24.04, skip to step 2.
   - **Ubuntu 24.04 VM** on a Windows laptop (Hyper-V/VirtualBox): 6 vCPU, 16 GB RAM (12 floor), 60 GB *fixed-size* virtual disk on SSD, **bridged** networking if fleet mode is ever planned.
1b. `git init` + private GitHub remote, all four teammates added; `git config core.autocrlf input` on every Windows-touching clone (LOG-008). Optional but recommended for the AIC↔home split: Tailscale on laptop + desktop; Syncthing on the Cowork folder (D-008).
2. Inside the VM, verify the kernel prerequisites **before** installing anything:
   ```bash
   uname -r                                  # ≥ 5.15
   ls /sys/kernel/btf/vmlinux                # must exist (eBPF CO-RE)
   stat -fc %T /sys/fs/cgroup                # must print: cgroup2fs
   cat /proc/pressure/cpu                    # must print some/full lines (PSI on)
   timedatectl | grep synchronized           # yes
   ```
3. Install K3s with our flags (add `--tls-san <tailscale-ip>,<magicdns-name>` on the desktop so kubectl works over the tailnet, D-008):
   ```bash
   curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik \
     --kubelet-arg=feature-gates=KubeletPSI=true" sh -
   sudo k3s kubectl get nodes   # Ready
   ```
4. Install tooling: `helm`, `k9s` (sanity), `docker`/`nerdctl` for image builds.
5. Verify PSI reaches Kubernetes (THE P0 gate):
   ```bash
   NODE=$(kubectl get nodes -o name | cut -d/ -f2)
   kubectl get --raw /api/v1/nodes/$NODE/proxy/metrics/cadvisor | grep -m3 container_pressure
   ```

**Done when:**
- [ ] all five kernel checks pass
- [ ] node Ready; `local-path` is default StorageClass (`kubectl get sc`)
- [ ] `container_pressure_*` lines visible from cadvisor endpoint
- [ ] LOG entry written (env specs recorded — exact RAM/CPU matters later for budget math)

**If broken:** no BTF file → wrong kernel (use stock Ubuntu generic kernel, not cloud-minimal); cgroup v1 → add `systemd.unified_cgroup_hierarchy=1` to GRUB; no pressure lines from cadvisor → typo in the feature-gate flag (check `/etc/systemd/system/k3s.service`), restart k3s.

**Rollback:** `/usr/local/bin/k3s-uninstall.sh` wipes everything; VM snapshot before P1 is mandatory.

**Desktop extras (on-demand GUI, LOG-010):** `sudo apt install --no-install-recommends xubuntu-core xrdp`, then `sudo systemctl set-default multi-user.target && sudo systemctl disable xrdp lightdm`. Start xrdp only when needed; never during rehearsal RAM measurements.

---

## P1 — L0 factory workloads (Days 1–4)

**Goal:** 15 pods (MASTER_PLAN §2.2 roster) running steady-state, honest pathologies armed but dormant.

**Order of build (each pod: image → chart entry → deploy → watch 10 min):**

1. **Spine first:** mqtt-broker (Mosquitto) → plc-gateway → telemetry-ingest → timescaledb → critical-control-relay → safety-interlock. Smoke: `mosquitto_sub -t 'sensors/#'` shows 10 Hz; DB row count grows; CCR heartbeat ticks.
2. **Storage trio:** shared-logs-pvc (RWO) → cooling-monitor, dcim-bridge, log-archiver mounting it with `podAffinity` co-location (§2.8-B2). Verify all three Running with the same `spec.nodeName`.
3. **Pathology carriers:** analytics-batch (CronJob */5), vision-qc (leak flag **off** via env `LEAK_ENABLED=false`), alert-dispatcher → notify-gateway chain.
4. **Bystanders:** edge-ui, firmware-cache (tmpfs emptyDir).

**Conventions:** every image multi-stage, tagged `:v0.X`, pushed to local registry (`k3s ctr images import` later for air-gap); every pathology parameter a Helm value (`leakRateMBs`, `fioSizeGB`, `cronCadence`, CPU limits); every pod has liveness+readiness probes per §2.4; labels: `app`, `tier: {core|data|edge}`, `pathology: {none|cpu|mem|io|net}`.

**Done when:**
- [ ] 15/15 Running, 0 unplanned restarts over 1 h
- [ ] steady-state telemetry visibly alive (MQTT ticks, DB inserts, periodic queries)
- [ ] `kubectl top pods -A` total L0 ≈ 3 GB RAM ±20%
- [ ] storage trio co-located on one node
- [ ] manual pathology smoke: exec fio in cooling-monitor for 30 s — node disk util spikes; vision-qc leak flag on → RSS climbs → flag off
- [ ] LOG entries per pod group

**If broken:** PVC Pending → local-path provisioner pod logs; CrashLoop on DB → fsGroup/permissions on the PVC mount; CronJob never fires → check timezone in schedule.

---

## P2 — Telemetry stack (Days 3–5)

**Goal:** every plane-3 tap (MASTER_PLAN §2.7) live in Prometheus/Loki.

**Steps:**

1. `kube-prometheus-stack` Helm (Grafana subchart **enabled during build** as debug lens — disabled at P8): retention 12h, two extra scrape jobs: `l0-fast` (5s, namespaces factory-*) and `truth` (5s, the ground-truth `/metrics`, label `channel=truth`).
2. `loki-stack` with **Grafana Alloy** (not Promtail — D-era note in plan §1.2) shipping all factory-* + observability logs.
3. **Caretta** Helm → confirm `caretta_links_observed_total` populates and the Grafana node-graph redraws plane 1 (screenshot it — report artifact: "discovered, not configured").
4. **OBI/Beyla** DaemonSet scoped to factory namespaces → RED metrics for alert-dispatcher→notify-gateway and edge-ui→firmware-cache appear.
5. **Inspektor Gadget** `kubectl gadget deploy`; dry-run `kubectl gadget top blockio` while fio runs.
6. Tune scrape load: `scrape_duration_seconds` p99 < 1 s; total Prometheus RSS < 800 MB.

**Done when:**
- [ ] PromQL returns data for every row of the §2.7 tap table (8 checks — script them: `appendix/verify_taps.sh`)
- [ ] PSI per-container queryable: `rate(container_pressure_io_stalled_seconds_total{namespace="factory-data"}[30s])`
- [ ] Caretta map == plane-1 diagram (minus rare edges) — screenshot saved
- [ ] Loki: `{namespace="factory-core"}` streams live
- [ ] observability namespace total < 2.5 GB RAM
- [ ] LOG entry incl. measured RAM per component (feeds D-001 review — if Prometheus > 1 GB here, that's the VM-contingency trigger)

**If broken:** Caretta empty → kernel/BTF mismatch (fall back: Otterize network-mapper, log a REVERT); OBI sees nothing → check it's not filtering to HTTPS-only ports; cadvisor 5s job missing pods → relabel config namespace regex.

---

## P3 — L2 aggregator (Days 5–6)

**Status: DONE 2026-06-13 (LOG-042).** Deployed to ns `aiops` (`skn/aggregator:v0.1`); on S1 it emits a schema-conformant `anomaly_candidate` (timescaledb psi_io 0.268 > 0.15) and serves per-pod vectors at `/window`.

**Goal:** Go service emitting schema-frozen JSON events + `GET /window` vectors.

**Steps:**

1. Scaffold `aggregator/` (Go 1.23): config-map-driven query pack (~25 PromQL strings — start from MASTER_PLAN Appendix A), 5s clock-aligned ticker, Loki tail for ERROR/WARN counts.
2. Implement the three outputs: `anomaly_candidate` events (threshold rules §1.3-3), 15-min ring buffer (180 samples × pod × signal), `GET /window` + `POST /events` (to L3) endpoints.
3. **Freeze the Event schema v1** (MASTER_PLAN §1.3.3) — after this point schema changes require a LOG `DECISION`.
4. Golden test: replay a recorded Prometheus window (fixture JSON) → assert identical event output (this test is what makes refactors safe later).

**Done when:**
- [ ] events flow during a manual fio burst within ≤ 10 s of breach; none at idle (24 h soak overnight)
- [ ] `/window` returns complete vectors, gaps ≤ 2 samples interpolated, RSS < 150 MB
- [ ] golden replay test green in CI (or just `make test`)
- [ ] LOG entry; schema v1 tagged in git

---

## P4 — Correlation & Dependency Engine (Days 6–9) — the heart

**Status: DONE 2026-06-14 (LOG-056).** Engine kernel 13/13; service (`skn/correlation-engine:v0.1`, ns `aiops`, `/graph`). On S1 it ranks the source **cooling-monitor #1** with a threshold-free causal edge to the victim **timescaledb** (`r=0.69, lag=30s, evidence=[stat, pvc, temporal]`) plus a blast radius. How it got robust: detection scans the FULL ring and correlation is **event-centred** on the detected disturbance (not a fixed recent slice); pairs are admitted by statistical correlation + physical-witness topology (no resource thresholds); `/window` samples are time-aligned by timestamp before correlating; and the DB baseline was quieted (plc-gateway rate cut) so the victim onsets cleanly. Remaining enrichment, not blockers: dcim-bridge as a co-victim, and the OBI latency hop to critical-control-relay.

**Goal:** MASTER_PLAN §1.4 in code: A1–A5 inference agents wired in LangGraph, S1 chain reproduced ≥ 8/10, S0 silent.

**Build inside-out (each step independently testable):**

1. **A1 Resource Agent** — EWMA+CUSUM on synthetic fixtures first (unit tests with planted onsets: detect within ±1 sample), then live `/window` data. Then the shape classifier (decision tree over slope/ACF/kurtosis/plateau) on the five planted pattern fixtures (burst/leak/saturation/throttle/flap).
2. **A3 Topology Agent** — consume Caretta + kube-state relations → steady topology graph; edge-health EWMA; serialize. Verify: topology == plane-1+2 within minutes of start.
3. **A4 Correlator** — lag scan vs analytic fixtures (two sine bursts lagged 15 s → expect peak at 15 s); then the §1.4.4 evidence gate (unit-test all three clauses incl. the same-node PSI rule); then explanatory-reach ranking (LOG-014: replaced PageRank) + blast radius on a hand-built chain with known answer. **Status: done 2026-06-12 — 13/13 tests green in `correlation/` (built ahead of schedule in the Cowork sandbox).**
4. **A2 Log Detective** — Drain3 over Loki window; Poisson surprise per template; novelty flag. Fixture: inject 50 fake OOM lines → template found, rate_z > 3.
5. **A5 Verdict** — evidence-weighted scorer + the 3 bounded K8s tool calls (events, restarts, IG blockio) via read-only ServiceAccount.
6. **LangGraph wiring** — fan-out A1–A3, join A4, gate A5 (§1.4.6); checkpoint to SQLite; idle tick 30 s; `anomaly_candidate` trigger path.
7. **Live fire:** manual S1 (exec fio) ×10 → measure: root-cause = cooling-monitor ≥ 8/10, chain ≥ 3 hops, end-to-end < 10 s (no LLM yet). Tune gate thresholds via Helm-style config, never code edits — and log every threshold change.

**Done when:**
- [ ] unit suites green for A1–A5 (fixtures committed)
- [ ] S1 manual ×10: ≥ 8 correct, zero false root cause
- [ ] S0 idle 30 min ×3: zero causal edges
- [ ] full pass < 2 s; LangGraph trace replayable for any incident
- [ ] LOG entries incl. final gate thresholds (these numbers go in the report's methodology)

**If broken:** chain truncates at DB hop → probe-timeout link too weak, lower DB probe `timeoutSeconds` or raise fio intensity (log the tuning); spurious edges between CronJob-periodic pods → §1.4.4-1 adjacency requirement too loose, raise to 0.45; PSI witness never fires → check cgroup driver and that pods share the node.

---

## P5 — Language layer (Days 9–10)

**Goal:** Ollama-backed narration with citation discipline and a template fallback that makes the demo model-proof.

**Steps:**

1. Ollama pod (`aiops` ns, memory limit 5 Gi, `keep_alive=30m`), model: 4B-class instruct Q4. Pull once, then bake into image/volume for air-gap.
2. Prompt = system contract (cite-or-die, scheduler verbs) + `Verdict` JSON only. Output JSON-schema-constrained (`Insight`).
3. Citation validator: every sentence ≥ 1 evidence ID; fail → regenerate once → Jinja template fallback.
4. Warm-on-anomaly: L2's first `anomaly_candidate` triggers a model load (10–20 s head start, §1.5).
5. Operator Q&A endpoint (`POST /ask`): question + current graph → grounded answer (demo beat 4).

**Done when:**
- [ ] S1 narration correct + cited, 10/10 runs (template fallback counts — note which path ran)
- [ ] LLM adds < 15 s on CPU-only; total trigger→insight < 30 s (the stopwatch claim)
- [ ] kill Ollama mid-incident → template insight still renders (model-proof check)
- [ ] LOG entry: model chosen, tokens/s measured, fallback rate

---

## P6 — Dashboard (Days 9–12)

**Goal:** the six panels (MASTER_PLAN §1.6), WebSocket-live, scenario console wired.

**Steps:**

1. Go backend: `/api/graph`, `/api/timeline`, `/api/insights`, `WS /live`, `POST /api/scenario/{id}` (applies Chaos CR / toggles leak env / triggers CronJob).
2. Next.js static-export served by the Go binary. Build order: causal graph (React Flow — node size/edge width/lag badges/animated propagation) → timeline (Recharts, anomaly shading, replay slider) → pod drawer (8 sparklines incl. PSI) → PSI heatmap → insight feed (evidence links open the drawer at the cited window) → scenario console.
3. Latency: graph update render < 1 s after L3 push; WS reconnect logic (demo networks misbehave).

**Done when:**
- [ ] S1 fired from the **console button** plays the full §2.6 sequence with stopwatch overlay
- [ ] evidence links navigate correctly 10/10
- [ ] 30-min soak: no WS leak (browser memory flat)
- [ ] LOG entry + screen recording of one full run (first golden-run candidate)

---

## P7 — Chaos conductor & scenario library (Days 11–12)

**Goal:** S0–S5 as version-controlled, one-click, resettable YAML.

**Steps:**

1. Install Chaos Mesh (containerd socket flag for K3s). 
2. Author `scenarios/S{0..5}/`: chaos CR (or native trigger), runbook.md (timeline table, expected witnesses, expected NLP, reset), `reset.sh`.
3. Map console buttons → scenario IDs; cooldown interlock (no overlapping scenarios unless explicitly testing S1+S3 dual-cause).
4. Rehearse: S1 ×20 (across reboots, cold/warm model), S2–S5 ×5 each, S0 ×3. Record pass/fail in a rehearsal ledger (`scenarios/ledger.csv`) — this becomes the report's accuracy table vs ground truth (D-004).

**Done when:**
- [ ] every scenario: fire → detect → narrate → reset → baseline, no manual kubectl
- [ ] rehearsal ledger ≥ 35 runs with outcomes
- [ ] S4 NetworkChaos verified under flannel (if it fights K3s networking, fall back to toxiproxy sidecar on notify-gateway — log a REVERT if so)
- [ ] LOG entry: success rates per scenario

---

## P8 — Hardening, air-gap, demo (Days 13–14)

**Steps:**

1. Disable build-time Grafana; pin all image digests; `k3s ctr images import` tarball built and tested on a **wiped** VM snapshot (the true air-gap rehearsal).
2. Resource limits audit vs §1.7 budget; `kubectl top` during S1 peak ≤ 75% node RAM.
3. Dress rehearsal ×5 with the §5.5 script + stopwatch; record the golden run video (backup if live demo dies).
4. Freeze: tag `v1.0-round2`; LOG entry `NOTE: freeze`; no changes after this without a `DECISION`.
5. Technical report assembly — pull directly from: MASTER_PLAN (architecture/methodology), BUILD_LOG (challenges/decisions timeline), rehearsal ledger (accuracy numbers), Caretta screenshot + golden video stills.

**Done when:** §5.4 verification checklist in MASTER_PLAN is fully ticked.

---

## Lost-finder — symptom → where to go

| Symptom | Go to |
|---|---|
| "What was the architecture decision about X?" | BUILD_LOG decision register, then MASTER_PLAN section it links |
| PSI metrics missing | P0 step 5 / P0 if-broken |
| A scenario stopped reproducing | P7 runbook for that scenario; check rehearsal ledger for when it last passed; diff Helm values since |
| Correlation edges look wrong | P4 if-broken; §1.4.4 thresholds in config; log every tuning |
| RAM creeping on the node | §1.7 budget table; P2 done-when measurements; D-001 contingency (VM swap) |
| LLM output garbage / slow | P5 — fallback path should already be carrying you; check citation validator logs |
| Demo box has no internet and something won't start | P8 step 1 — image missing from tarball; `k3s ctr images ls | grep <name>` |
| "Why did we change X?" | BUILD_LOG, search the component name — if it's not logged, that's the bug; log it now |
| Genuinely lost | Read MASTER_PLAN §0 (the bar), then the current phase's Done-when, work backwards |

*Guide v1.0 — 2026-06-12. Update via LOG entries; bump version on structural change.*
