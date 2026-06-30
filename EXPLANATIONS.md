# EXPLANATIONS — how the system works, file by file

This is the "how it works" reference: the idea, the end-to-end flow, and every significant
editable file — what it is, what it does, and how it functions. Register: keep the technical
terms (pod, PVC, PSI, CUSUM, Pearson, eBPF) but explain in narrative. The *decision history*
(why each choice was made, in order) lives in `BUILD_LOG.md`; the *phase plan* in
`BUILD_GUIDE.md`; the *architecture spec* in `MASTER_PLAN.md`. This file is the map of the code.

---

## 1. The idea in one breath

A single Kubernetes node runs hundreds of pods sharing one CPU, memory, disk, and set of
storage volumes. When something slows down, ordinary tools (`kubectl top`, dashboards) show you
*what* is hot — never *who made it hot* or *who it will hurt next*. We watch the cluster from
the outside, change nothing inside the applications, and turn raw kernel signals into a causal
story: *"this pod's disk storm is starving that pod, which is why a third pod misses its
deadline."* The reasoning is deterministic math. Exactly one language model exists in the whole
system, and it only writes the final sentence, citing evidence the math already found.

The headline signal is **PSI (Pressure Stall Information)** — a number the Linux kernel keeps
for every container: the fraction of time a pod is *stalled waiting* for CPU, memory, or I/O.
Utilization says a pod is "busy"; PSI says it is "suffering." That difference is what lets us
claim one pod is being *hurt* by another, even when they never talk over the network — the
blind spot of every service-mesh tool.

## 2. The shape of the system (L0 → L4)

```
 L0  factory (15 pods)        the system we watch — produces REAL faults (fio, OOM, throttle)
  │   kernel mechanisms
 L1  telemetry               Prometheus scrapes kubelet/cAdvisor every 5s: PSI, cgroup, eBPF
  │   PromQL
 L2  aggregator (Go)         PromQL -> one frozen JSON event shape; 15-min per-pod ring at /window
  │   /window + /events
 L3  correlation engine (Py) detect -> correlate -> gate -> rank -> forecast  ==>  /graph
  │   /graph (causal graph)
 L4  narrator + dashboard    one local LLM writes the verdict; UI shows the graph + scenarios
```

Generation → collection → **interpretation** → presentation. Each layer is independently
testable, and the contract between layers is a small, stable JSON shape.

## 3. End-to-end: what happens when you fire S1

S1 is "PVC I/O contention cascade." Trace one button-press through the files:

1. **`scenarios/S1/trigger.sh`** touches a `FLUSH` flag (or POSTs `:8080/flush`) on
   **cooling-monitor**.
2. **`workloads/cooling-monitor/main.py`** sees the flag and runs a real, sustained `fio`
   storm against `/shared/cooling` — a directory on `shared-logs-pvc`, a volume that lives on
   the **same physical disk** as the database's `tsdb-pvc`.
3. The kernel's I/O scheduler does the rest: **timescaledb** — a *different* pod with no network
   link to cooling-monitor — starts stalling on that saturated disk. Its `psi_io` climbs.
4. **`aggregator/main.go` (L2)** is polling Prometheus every 5s using
   **`aggregator/queries.yaml`**; it sums `psi_io` per pod, sees timescaledb cross the
   threshold, emits an `anomaly_candidate` event (shape frozen by
   **`aggregator/event.schema.json`**), and keeps the last 15 minutes of every pod's signals in
   a ring served at `/window`.
5. **`correlation/service.py` (L3)** polls `/window` and `/events`, time-aligns every pod onto a
   shared clock, and calls `run_pass`.
6. **`correlation/engine/pipeline.py`** orchestrates the verdict using the four kernel modules:
   `detectors.py` finds each pod's onset, `lagcorr.py` measures who-leads-whom, `gate.py`
   admits an edge only with real evidence, `ranking.py` names the root cause and the blast
   radius. The result is served at `/graph`.
7. On a clean run the graph reads: **root = cooling-monitor**, edge **cooling-monitor →
   timescaledb** (`evidence = [stat, pvc, temporal]`), blast radius = timescaledb. Source
   correctly blamed, victim correctly predicted, **no resource threshold anywhere in the causal
   path** — only correlation, shared-disk topology, and time order.

## 4. The files, layer by layer

### L0 — the factory (`workloads/`, one folder per pod, each with its own Dockerfile)

The honesty rule for all of L0: **every fault is a real kernel mechanism**, never a faked
metric. If we injected numbers we'd only be testing our own assumptions; by producing the real
physics, the tool has to *discover* the story.

- **`plc-gateway/main.go`** — fakes the sensor floor: publishes `PLC_CHANNELS` (200) channels at
  `1000 / PLC_PERIOD_MS` Hz to MQTT. Both are env-tunable (no rebuild of behaviour, just the
  values). The publish rate *is* the database's write-load dial: it was cut from 10 Hz to 1 Hz
  (≈2000 → 200 rows/s) so timescaledb idles with headroom and only stalls under a storm — the
  precondition for it being a *clean* victim rather than a permanently-saturated one.
- **`mqtt-broker`** — Mosquitto; the message bus every sensor reading passes through.
- **`telemetry-ingest/main.py`** — drains `sensors/#` from MQTT and **batch-INSERTs** into
  TimescaleDB (up to 500 rows or 1s per commit, so ~4 commits/s — deliberately batched). Exposes
  `ingest_queue_depth`: the queue rises when the DB slows, which is the visible S1 back-pressure.
- **`timescaledb/init.sql` + Dockerfile** — the `readings` hypertable (ts, topic, payload) with
  native compression after 1h and a 14-day retention policy (the rolling demo history). Its data
  lives on `tsdb-pvc`. **init.sql is baked into the image** — changing it needs an image rebuild,
  not just a redeploy.
- **`cooling-monitor/main.py`** — steady state: a light thermal journal to `/shared/cooling`. On
  trigger (FLUSH flag *or* `POST :8080/flush`): a sustained, fsync-heavy `fio` storm, intensity
  set by env `FIO_SIZE/JOBS/RUNTIME/FSYNC/DIRECT` (no rebuild to retune). This is S1's source.
- **`dcim-bridge/`** — writes to `/shared/dcim` on the **same** `shared-logs-pvc`; the
  first-in-line disk victim, and a co-victim in the S1 fan-out.
- **`critical-control-relay/`** — the latency-sensitive actuator with a 100 ms SLO and an HTTP
  health probe; the pod every cascade eventually hurts (the 4th hop, via OBI latency).
- **`safety-interlock/`** — trips to safe-mode if the control relay's heartbeat misses.
- **`log-archiver/` (CronJob)** — tars logs on demand (S2); **`analytics-batch/` (CronJob)** —
  CPU-heavy rollups on demand (S3); both `suspend: true` so they fire only on trigger.
- **`vision-qc/`** — "defect detection"; with `LEAK_ENABLED=true` it grows memory to its limit
  and the OOM-killer fires (S5).
- **`notify-gateway/`, `alert-dispatcher/`, `edge-ui/`, `firmware-cache/`** — the edge tier
  (alerts + kiosk); mostly steady-state ballast, with `firmware-cache` carrying a tmpfs volume.

### L1 — telemetry (`deploy/values/`, installed by skctl)

No application is instrumented; everything is read from the kernel via the kubelet.

- **`prometheus.yaml`** — kube-prometheus-stack values. Scrapes kubelet **cAdvisor** every 5s
  for per-container CPU/mem/throttle and the differentiator, **PSI**. A `channel=truth` relabel
  fences each app's *own* `/metrics` out of the engine's view (keeps "zero instrumentation"
  honest); Grafana is disabled (a crashloop on this image) and Prometheus runs on emptyDir.
- **`loki.yaml` + `alloy.yaml`** — log pipeline (Alloy ships pod logs to Loki). Deferred red.
- **`caretta.yaml` + `beyla.yaml`** — eBPF add-ons: Caretta's who-talks-to-whom service map and
  OBI/Beyla request latency (the `latency_p95` signal that lights up the control-relay hop).
  These are the deferred eBPF items — the core L0→L3 causal path needs none of them.

### L2 — aggregator (`aggregator/`, Go, deployed to ns `aiops`)

The firewall between raw Prometheus text and the brain — the brain never sees a wall of metrics,
only clean typed events.

- **`main.go`** — every `interval_s` it runs each query in the pack, stamps each sample with the
  poll time, appends to a per-pod ring (`capN` = 15 min / interval), and on a threshold breach
  emits an `anomaly_candidate`. Serves `/window` (the ring, for L3), `/events` (recent
  anomalies), `/healthz`. PSI is summed **per pod** (`sum by (namespace, pod)`), the fix that
  made events actually fire. *Note for L3:* the ring is a positional append and samples can drift
  in time across pods — which is why L3 re-aligns them by timestamp (see `service.py`).
- **`queries.yaml`** — the PromQL "pack": one query per signal (cpu, psi_cpu/mem/io, **io_write**
  = per-pod disk-write throughput, the source-attribution signal, mem, net, pvc, restarts,
  latency_p95…) plus the `thresholds` block. ConfigMap-mounted, so editing queries/thresholds needs
  only a configmap reload + restart, no image rebuild. **These thresholds are an L2 alerting hint
  only — the L3 causal graph does not depend on them.**
- **`event.schema.json`** — the FROZEN v1 event contract (`v/kind/ns/pod/signal∈enum/value/
  zscore/threshold/window_s`). Freezing it lets L2 and L3 evolve independently.

**L2 durability note.** Current code keeps `/window` as a live 15-minute ring. The planned 14-day
L2 store should persist the same samples/events by absolute `ts` and let L3 bootstrap from
`ORDER BY ts` when memory is empty or stale. That rolling telemetry DB is not the engine's
long-term memory.

### L3 — correlation engine (`correlation/`, Python, deployed to ns `aiops`)

The deterministic detective. **No LLM anywhere in this layer.** Five ideas, five files.

- **`service.py`** — the I/O shell. Polls `/window` + `/events`, then **`build_inputs`** does the
  crucial pre-processing: it resamples every pod onto **one shared wall-clock grid by each
  sample's timestamp** (positional indices drift because PSI is gappy and pods restart), and
  drops stale/dead pods automatically. It builds the physical-witness sets (which pods share a
  disk, which are co-stalled), runs `run_pass`, and serves the result at `/graph`. Key env:
  `ENGINE_SIGNAL` (psi_io), `ANALYSIS_WINDOW` (correlation span), `POLL_S` (grid step).
- **`engine/state.py`** — persistent evolutionary memory + self-calibration, keyed by **workload**
  (not the ephemeral pod-hash, so confidence survives restarts). It stores: edge confidence with a
  learned **structural floor** (a witnessed coupling settles to a faint baseline instead of
  vanishing, and brightens under load); similarity-merged **case families** (a variation is
  recognised as a *variant of a type*, not a new type); per-workload **PSI baselines** (median+MAD,
  storm-skipped) that define "normal" so an onset only counts as an incident when it **deviates**
  from it — this is why S0 is silent; plus graph snapshots, model versions, and mistake records.
  Mounted on `engine-memory-pvc`, no 14-day TTL. The pure `run_pass` output is the live evidence;
  `state.py` decides how knowledge persists, fades, **gates incidents**, and is promoted into cases.
- **`engine/detectors.py` (A1)** — changepoints. An **EWMA** tracks "normal"; a **CUSUM**
  accumulates drift and fires only when it's *sustained*, giving an onset accurate to a sample.
  `classify` then names the shape — burst (rises, returns), leak (keeps climbing), saturation
  (pinned at a limit), flap (oscillation/restart loops), shift. Robust σ from a longer quiet
  prefix so jitter doesn't fake onsets.
- **`engine/lagcorr.py` (A4)** — who leads whom. Pearson r (Spearman fallback for heavy tails)
  between two pods at shifted alignments (0/5/15/30/60/120 s); the shift with the strongest |r|
  is the lag, and its sign is the direction. "cooling-monitor at T matches timescaledb at T+30s"
  → cooling-monitor leads.
- **`engine/gate.py` (the false-positive killer)** — an edge enters the graph only if **all
  three** hold: (1) statistical — **positive** |r| ≥ 0.6 at the peak *and* adjacent-lag support
  (anti-correlation is competition, not a cascade); (2) **physical coupling** — a shared PVC or an
  eBPF link (PSI co-pressure only *corroborates*; it never makes an edge); (3) temporal — the
  source's onset precedes the victim's, consistent with the lag. Correlation alone never makes an
  edge. (Cross-signal **source** edges — write→staller — apply the same gate; see `pipeline.py`.)
- **`engine/ranking.py` (A5)** — root cause by **explanatory reach**: walk accepted edges
  forward from each candidate with decaying weight, sum how much of the symptom set it explains,
  penalize anyone who is themselves explained from upstream. Top score = root cause; the same
  forward walk yields the **blast radius** with ETAs. (Deterministic and narratable — not
  PageRank, which let the victim outrank its cause.)
- **`engine/pipeline.py` (`run_pass`, the orchestrator)** — ties it together, and carries the
  ideas that made it work on real data:
  - **Deviation-gated detection** — an onset is an incident only if the pod's sustained (p90) PSI
    exceeds its learned baseline (passed in from `state.py`); normal factory load stays silent (S0).
  - **Event-centred analysis** — detect across the **full** ring (find the disturbance wherever it
    sits, with a clean pre-event baseline), then correlate a slice **centred on the detected event**,
    so a storm minutes old is still analysed and dominates the correlation instead of being diluted.
  - **Source attribution (writer→staller)** — PSI sees only victims, so the *source* of a disk storm
    is found from the per-pod **write** signal: the **dominant** writer that actually **deviated**,
    positively correlated to (and leading) the victim's stall over the shared disk, oriented
    writer→staller — no lag coin-flip, no DB's baseline writes mistaken for a source.
  - **Threshold-free admission** — a coupled pair is evaluated once something is disturbed, pulling a
    victim in by correlation over the shared disk, never by an absolute resource limit. `run_pass`
    stays a pure function; `tests/test_engine.py` pins it (13 kernel fixtures + source/baseline/
    anti-correlation cases).

### L4 — narration + dashboard (planned, P5/P6)

A small local model (Ollama) renders the engine's verdict into a sentence, with a deterministic
template fallback; the dashboard shows the causal graph, a PSI heatmap, and scenario buttons.
The **API service** (`api/`, below) is the frontend-agnostic seam that feeds any UI.

### Deploy & ops (`deploy/`)

- **`skctl`** — the one bootstrap script. `up --mode solo` brings up namespaces → the factory
  Helm chart → telemetry → the L2/L3 deploys, idempotently. Also `pause`/`resume` (idle the
  factory between sessions) and `down`. In solo mode never pass `--components <subset>` — the
  flag is exclusive and disables unlisted groups (decision D-012).
- **`charts/factory/values.yaml`** — the single source of truth for the pod roster (name, group,
  namespace, image, CPU/mem, env, mounts, affinity), the two PVCs, and every tunable knob
  (FIO_*, PLC_PERIOD_MS, storageClass). Edit here, not in templates.
- **`charts/factory/templates/`** — `workloads.yaml` renders each pod/cronjob/service from the
  values list; `pvcs.yaml` renders the PVCs (storageClass defaults to `local-path`, kept across
  toggles by `helm.sh/resource-policy: keep`).
- **`slowdisk.yaml`** — static `local` PVs + a `slowdisk` StorageClass pinning the two factory
  PVCs to a dedicated spinning disk (`/dev/sdb`), so S1's contention happens on a slow disk where
  the source actually stalls — while the K3s control plane stays fast on the NVMe.
- **`aggregator.yaml` / `engine.yaml`** — the L2 and L3 Deployments+Services in ns `aiops`.
  `engine.yaml` also creates `engine-memory-pvc`, a small keep-annotated local-path PVC for L3's
  permanent memory; it is intentionally separate from the HDD-backed L0 storm volumes.
- **`appendix/*.sh`** — read-only diagnostics: `verify_taps` (the telemetry tap gate),
  `component_check` (P0–P2 sweep), `diag_scrape`, `restart_test`, `psi_watch`.

### Scenarios (`scenarios/`)

Each is version-controlled with a runbook + reset, and heavy load runs **only on trigger**:
S0 (idle — the engine must stay silent), **S1** (PVC I/O contention — the proven chain), S2
(large-file I/O), S3 (CPU throttle, no network path), S4 (network latency + retries), S5
(memory leak → OOM).

## 5. Knobs you can turn without an image rebuild

- **Engine:** `ANALYSIS_WINDOW` (correlation span), `ENGINE_SIGNAL`, `POLL_S`, `MEMORY_DB`,
  `EDGE_ALPHA`, `EDGE_DECAY`, `EDGE_SHOW`, `EDGE_HIDE` — env on `deploy/correlation-engine`.
- **S1 intensity:** `FIO_JOBS/RUNTIME/FSYNC/SIZE/DIRECT` — cooling-monitor env in `values.yaml`.
- **DB write load:** `PLC_PERIOD_MS`, `PLC_CHANNELS` — plc-gateway env in `values.yaml`.
- **L2 thresholds/queries:** `aggregator/queries.yaml` (ConfigMap; reload + restart).
- Anything baked into an image (init.sql, any `main.py`/`main.go`, `pipeline.py`) needs
  `docker build` + `k3s ctr images import` + a rollout restart.

## 6. The principles that make it defensible

1. **Zero application instrumentation** — every signal comes from the kernel via the kubelet.
2. **Real faults, not fake metrics** — fio storms, the OOM-killer, CFS throttling.
3. **Threshold-free causal path** — edges rest on correlation + physical-witness topology +
   temporal order, not "value > limit." Resource thresholds exist only as a coarse L2 alert hint.
4. **Search, don't poll** — the engine locates the disturbance in the stored series by detection,
   then analyses it; storm duration and check timing stop mattering.
5. **Deterministic core, one LLM at the edge** — the same input yields the same verdict on stage;
   the model only narrates evidence the math already produced.

> Decision history and the blow-by-blow of how each of these was arrived at: `BUILD_LOG.md`.
