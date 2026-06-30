#!/usr/bin/env python3
"""L3 correlation service (P4).

Polls the L2 aggregator's /window (per-pod signal vectors) and /events (anomaly
seeds), builds the engine inputs, runs one deterministic pass, and serves the
latest CausalGraph at /graph. No language model anywhere in this process; the
single LLM lives at L4.

v0 witness construction (until Caretta/OBI land): shared-storage relations come
from the known storage-domain workloads (one physical disk via local-path), and
PSI co-pressure comes from pods whose signal is elevated in the same window.
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from engine.gate import Witness
from engine.pipeline import run_pass
from engine.state import GraphMemory, MemoryConfig

WINDOW_URL = os.environ.get("WINDOW_URL", "http://aggregator.aiops.svc:9000/window")
EVENTS_URL = os.environ.get("EVENTS_URL", "http://aggregator.aiops.svc:9000/events")
SIGNAL     = os.environ.get("ENGINE_SIGNAL", "psi_io")          # representative signal for v0 (victims/stallers)
WRITE_SIGNAL = os.environ.get("WRITE_SIGNAL", "io_write")       # source/aggressor signal: who hogs the disk
INTERVAL   = int(os.environ.get("ENGINE_INTERVAL", "10"))        # seconds between passes
PORT       = int(os.environ.get("ENGINE_PORT", "9100"))
COPR_MIN   = float(os.environ.get("COPRESSURE_MIN", "0.10"))     # signal level that counts as "stalled"
ANALYSIS_WINDOW = int(os.environ.get("ANALYSIS_WINDOW", "36"))   # samples (~3min): the WHOLE pass (detect+correlate+order) looks back over the recent disturbance, not the 15-min ring. Match to event timescale; not a resource limit.
GRID_STEP_S = float(os.environ.get("POLL_S", "5"))               # aggregator scrape cadence = the time-alignment grid step (resample all pods onto a shared wall-clock axis)
MEMORY_DB  = os.environ.get("MEMORY_DB", "/var/lib/skn/memory/l3-memory.db")
STORAGE    = [s.strip() for s in os.environ.get(
    "STORAGE_WORKLOADS", "cooling-monitor,dcim-bridge,log-archiver,timescaledb").split(",")]

_memory = GraphMemory(
    MEMORY_DB,
    MemoryConfig(
        signal=SIGNAL,
        alpha=float(os.environ.get("EDGE_ALPHA", "0.4")),
        decay=float(os.environ.get("EDGE_DECAY", "0.1")),
        show=float(os.environ.get("EDGE_SHOW", "0.6")),
        hide=float(os.environ.get("EDGE_HIDE", "0.25")),
        prior=float(os.environ.get("EDGE_PRIOR", "0.2")),
        floor_frac=float(os.environ.get("EDGE_FLOOR_FRAC", "0.4")),
        tau_merge=float(os.environ.get("CASE_TAU_MERGE", "0.85")),
        tau_family=float(os.environ.get("CASE_TAU_FAMILY", "0.60")),
        base_alpha=float(os.environ.get("BASE_ALPHA", "0.05")),
        dev_k=float(os.environ.get("DEV_K", "4.0")),
        mad_floor=float(os.environ.get("MAD_FLOOR", "0.01")),
        base_min_n=int(os.environ.get("BASE_MIN_N", "12")),
    ),
)
_lock = threading.Lock()
_graph = _memory.bootstrap_graph()


def _fetch(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def workload(pod):
    """cooling-monitor-59584cbf7d-6szhd -> cooling-monitor (drop replicaset + pod hash)."""
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


def _epoch(ts):
    """Aggregator stamps each sample with its poll time (Go RFC3339). -> epoch seconds, or None."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def build_inputs(window, events):
    """window: {ns/pod/signal: [{ts,value,...}]}  ->  (vectors, write_vectors, witness, breach), TIME-ALIGNED.

    The aggregator ring is a positional append, but psi_io is gappy and pods restart,
    so column i drifts across pods (one pod's sample #100 can be a minute off another's).
    Resample every pod onto ONE shared wall-clock grid by its `ts`, so column k is the
    same instant for all pods -- the precondition lagged cross-correlation assumes.
    Stale pods (last sample older than the grid) drop out for free (retires LOG-048).
    """
    step, n = GRID_STEP_S, 180
    raw = {SIGNAL: {}, WRITE_SIGNAL: {}}              # collect psi (victims) + io_write (source) together
    latest = 0.0
    for key, samples in window.items():
        parts = key.split("/")
        if len(parts) < 3 or parts[-1] not in raw or not samples:
            continue
        pts = sorted((t, s["value"]) for s in samples if (t := _epoch(s.get("ts"))) is not None)
        if len(pts) >= 12:
            raw[parts[-1]][parts[1]] = pts
            latest = max(latest, pts[-1][0])

    grid = [latest - step * (n - 1 - k) for k in range(n)]

    def to_vectors(per_pod):                          # resample one signal's pods onto the shared grid
        out = {}
        for pod, pts in per_pod.items():
            if pts[-1][0] < latest - 2 * step:        # stale/dead pod -> drop (no recent data)
                continue
            vec, j = np.full(n, np.nan), 0
            for k, gt in enumerate(grid):             # sample-and-hold onto the shared grid
                while j + 1 < len(pts) and pts[j + 1][0] <= gt + step / 2:
                    j += 1
                if abs(pts[j][0] - gt) <= step:
                    vec[k] = pts[j][1]
            if np.count_nonzero(~np.isnan(vec)) >= 12:  # real coverage; a gap == no activity == 0
                out[pod] = np.nan_to_num(vec, nan=0.0)
        return out

    vectors = to_vectors(raw[SIGNAL])
    write_vectors = to_vectors(raw[WRITE_SIGNAL])

    pods = list(vectors)
    shared, copr = set(), set()
    hot = [p for p in pods if float(np.max(vectors[p][-6:])) > COPR_MIN]
    for i in range(len(pods)):
        for j in range(i + 1, len(pods)):
            a, b = pods[i], pods[j]
            if workload(a) in STORAGE and workload(b) in STORAGE:
                shared.add(frozenset((a, b)))            # same physical disk (local-path)
    for i in range(len(hot)):
        for j in range(i + 1, len(hot)):
            copr.add(frozenset((hot[i], hot[j])))        # single node => same PSI domain

    witness = Witness(ebpf_edges=set(), psi_copressure=copr, shared_relation=shared)
    breach = sorted({e["pod"] for e in events if isinstance(e, dict) and e.get("kind") == "anomaly_candidate"})
    return vectors, write_vectors, witness, breach


def loop():
    global _graph
    while True:
        try:
            window = _fetch(WINDOW_URL)
            events = _fetch(EVENTS_URL)
            vectors, write_vectors, witness, breach = build_inputs(window, events)
            if vectors:
                # per-pod incident threshold from the learned steady-state baseline (None while
                # still maturing) -> an onset is an incident only if it deviates from normal
                baselines = {pod: _memory.baseline_threshold(workload(pod)) for pod in vectors}
                out = run_pass(vectors, witness, slo_breach=breach or None,
                               window=ANALYSIS_WINDOW, write_vectors=write_vectors or None,
                               baselines=baselines)
                out["meta"]["signal"] = SIGNAL
                out["meta"]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                out = _memory.observe(out, vectors, witness=witness, ts=time.time())
                with _lock:
                    _graph = out
        except Exception as e:  # never die; report the error on /graph
            with _lock:
                _graph = {"meta": {"status": "error", "error": str(e)}}
        time.sleep(INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n")
        if self.path.rstrip("/") in ("", "/graph"):
            with _lock:
                return self._send(200, json.dumps(_graph).encode(), "application/json")
        self._send(404, b"not found\n")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    threading.Thread(target=loop, daemon=True).start()
    print(f"correlation engine up on :{PORT}; window={WINDOW_URL} signal={SIGNAL}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
