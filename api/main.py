"""L4 API gateway — a frontend-agnostic REST seam over the L2 aggregator + L3 engine.

It proxies and *normalizes* the causal graph, per-pod signals, and anomaly events into clean,
stable JSON with permissive CORS and an auto-generated OpenAPI spec at /docs — so any frontend
(React, Vue, a plain HTML page, a CLI) can consume the system without knowing the internal
service names or payload shapes. No causal logic lives here; the reasoning stays in L3. The one
transform it applies is collapsing live pod names (`cooling-monitor-6644486769-6wlst`) to stable
workload names (`cooling-monitor`) so a UI can key off something that survives restarts.

Env: ENGINE_URL, AGGREGATOR_URL, COOLING_URL, ENGINE_SIGNAL.
"""
import json
import os
import time
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

ENGINE = os.environ.get("ENGINE_URL", "http://correlation-engine.aiops.svc:9100").rstrip("/")
AGG = os.environ.get("AGGREGATOR_URL", "http://aggregator.aiops.svc:9000").rstrip("/")
COOLING = os.environ.get("COOLING_URL", "http://cooling-monitor.factory-data.svc:8080").rstrip("/")
SIGNAL = os.environ.get("ENGINE_SIGNAL", "psi_io")
SIGNAL_RESOURCE = {"psi_io": "disk I/O", "psi_cpu": "CPU", "psi_mem": "memory"}  # ground the narrator's resource word
# The one LLM. Unset OLLAMA_HOST -> /api/narrative serves the deterministic template only, so the
# verdict never depends on the model being reachable (the demo must survive a model outage).
OLLAMA = os.environ.get("OLLAMA_HOST", "").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

app = FastAPI(
    title="SiliconKnights Edge Causal AIOps API",
    version="1.0",
    description="Frontend-agnostic REST over the causal correlation engine (L3) and the "
                "telemetry aggregator (L2). Read endpoints under /api; OpenAPI at /openapi.json.",
)
# Permissive CORS so a separately-served frontend (any origin/port) can call this directly.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _post(url, timeout=8):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def _ollama(prompt, timeout=30):
    """One non-streamed completion from Ollama; None on any failure so the caller falls back.
    `think: false` disables gemma's reasoning phase (we want one fast, deterministic sentence)."""
    if not OLLAMA:
        return None
    body = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                       "think": False, "options": {"temperature": 0.2}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.load(r).get("response") or "").strip() or None
    except Exception:
        return None


def _template_narrative(g) -> str:
    """Deterministic verdict sentence built from the graph — the always-available fallback."""
    root = g.get("root") or []
    edges = g.get("edges") or []
    meta = g.get("meta") or {}
    signal = meta.get("signal", SIGNAL)
    if not root or not edges:
        return f"Steady state: no causal contention detected across {meta.get('pods', 0)} workloads."
    cause = root[0]["pod"]
    out = [e for e in edges if e["src"] == cause] or edges          # strongest edge leaving the root
    e = max(out, key=lambda x: abs(x.get("r") or 0.0))
    victim = e["dst"]
    ev = ", ".join(e.get("evidence") or []) or "correlation"
    eta = {b["pod"]: b.get("eta_s") for b in (g.get("blast_radius") or [])}.get(victim)
    eta_txt = f", with impact on {victim} expected in ~{int(eta)}s" if eta else ""
    reg = meta.get("case_register")
    reg_txt = f" (recognised as a {reg} of a known case)" if reg in ("recurrence", "variant") else ""
    return (f"{cause} is the likely root cause of {signal} contention: its activity correlates with "
            f"{victim} over {ev}{eta_txt}{reg_txt}.")


def workload(pod: str) -> str:
    """cooling-monitor-6644486769-6wlst -> cooling-monitor (drop replicaset + pod hash)."""
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


SCENARIOS = [
    {"id": "S0", "name": "Steady-state control", "mechanism": "10 min idle; expect no causal edges", "triggerable": False},
    {"id": "S1", "name": "PVC I/O contention cascade", "mechanism": "sustained fio on a shared volume", "triggerable": True},
    {"id": "S2", "name": "Large-file I/O starvation", "mechanism": "bulk archive read/write", "triggerable": False},
    {"id": "S3", "name": "CPU throttle interference", "mechanism": "CPU-bound burst under a constrained limit", "triggerable": False},
    {"id": "S4", "name": "Network degradation + retry amplification", "mechanism": "injected egress latency", "triggerable": False},
    {"id": "S5", "name": "Memory leak + OOM termination", "mechanism": "unbounded growth to the memory limit", "triggerable": False},
]


@app.get("/api/health", tags=["meta"])
def health():
    """Reachability of the upstream L2/L3 services."""
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "services": {}}
    for name, url in (("aggregator", AGG + "/healthz"), ("engine", ENGINE + "/healthz")):
        try:
            urllib.request.urlopen(url, timeout=3)
            out["services"][name] = "up"
        except Exception:
            out["services"][name] = "down"
    out["ok"] = all(v == "up" for v in out["services"].values())
    return out


@app.get("/api/graph", tags=["causal"])
def graph():
    """The current causal verdict from L3 — root cause, edges (with evidence), blast radius,
    findings — with pod names normalized to stable workload names for the UI."""
    try:
        g = _get(ENGINE + "/graph")
    except Exception as e:
        raise HTTPException(503, f"engine unreachable: {e}")
    w = workload
    return {
        "root": [{"pod": w(r["pod"]), "score": r.get("score"), "onset_s": r.get("onset_s")}
                 for r in g.get("root_cause_ranking", [])],
        "edges": [{"src": w(e["src"]), "dst": w(e["dst"]), "r": e["r"], "lag_s": e["lag_s"],
                   "evidence": e["evidence"], "confidence": e.get("confidence"),
                   "state": e.get("state"), "render_weight": e.get("render_weight"),
                   "source": e.get("source")} for e in g.get("edges", [])],
        "blast_radius": [{"pod": w(b["pod"]), "impact": b["impact"], "eta_s": b["eta_s"]}
                         for b in g.get("blast_radius", [])],
        "findings": [{"pod": w(f["pod"]), "class": f.get("class"), "onset_s": f.get("onset_s"),
                      "severity": f.get("severity")} for f in g.get("findings", [])],
        "meta": g.get("meta", {}),
    }


# Cache the LLM verdict keyed by the graph's shape, so the dashboard's 5s poll doesn't re-run the
# model every tick — we only regenerate when the verdict actually changes.
_NARR_CACHE: dict = {}


def _verdict_signature(g) -> str:
    root = g.get("root") or []
    edges = g.get("edges") or []
    return json.dumps(
        {"root": root[0]["pod"] if root else None,
         "edges": sorted((e["src"], e["dst"], e.get("state")) for e in edges),
         "case": (g.get("meta") or {}).get("case_register")},
        sort_keys=True,
    )


@app.get("/api/narrative", tags=["causal"])
def narrative():
    """One-sentence operator verdict. A local LLM (Ollama) renders the causal graph into prose
    that cites the evidence the engine already found; it falls back to a deterministic template
    when the model is unset/unreachable/slow — so the verdict never depends on the model."""
    g = graph()  # normalized verdict; raises 503 if the engine is unreachable
    sig = _verdict_signature(g)
    if sig in _NARR_CACHE:
        return _NARR_CACHE[sig]
    template = _template_narrative(g)
    resource = SIGNAL_RESOURCE.get((g.get("meta") or {}).get("signal", SIGNAL), "resource")
    prompt = (
        "You are an SRE assistant. Given this causal verdict JSON from a Kubernetes resource-"
        f"contention engine, write ONE or TWO plain sentences for an on-call operator. The contended "
        f"resource is {resource}; call it {resource} contention and do NOT name any other resource "
        "type (not memory, not CPU). The root-cause pod is the SOURCE; the blast-radius pods are the "
        "affected VICTIMS. Cite only the evidence types and ETAs present in the JSON; do not invent "
        "metrics, numbers, or causes. If there is no root cause, say the system is steady.\n\n"
        "VERDICT:\n" + json.dumps({k: g.get(k) for k in ("root", "edges", "blast_radius", "meta")})
    )
    text = _ollama(prompt)
    out = {
        "text": text or template,
        "source": "llm" if text else "fallback",
        "model": OLLAMA_MODEL if text else None,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if text:  # cache only successful LLM renders; while it falls back, keep retrying each poll
        _NARR_CACHE.clear()
        _NARR_CACHE[sig] = out
    return out


@app.get("/api/pods", tags=["telemetry"])
def pods():
    """Live per-workload snapshot: most-recent level of the engine signal + whether the engine
    currently considers it anomalous. Sorted hottest-first, for a heatmap or a status list."""
    try:
        window = _get(AGG + "/window")
    except Exception as e:
        raise HTTPException(503, f"aggregator unreachable: {e}")
    try:
        flagged = {workload(f["pod"]) for f in _get(ENGINE + "/graph").get("findings", [])}
    except Exception:
        flagged = set()
    out = {}
    for key, samples in window.items():
        parts = key.split("/")
        if len(parts) < 3 or parts[-1] != SIGNAL or not samples:
            continue
        w = workload(parts[1])
        cur = out.setdefault(w, {"workload": w, "namespace": parts[0], "signal": SIGNAL, "value": 0.0, "anomalous": False})
        cur["value"] = round(max(cur["value"], float(samples[-1]["value"])), 4)
        cur["anomalous"] = w in flagged
    return sorted(out.values(), key=lambda p: -p["value"])


@app.get("/api/signal/{pod}", tags=["telemetry"])
def signal(pod: str, signal: str = SIGNAL):
    """Raw (ts, value) time series for one workload's signal — for charting a single pod."""
    try:
        window = _get(AGG + "/window")
    except Exception as e:
        raise HTTPException(503, f"aggregator unreachable: {e}")
    for key, samples in window.items():
        parts = key.split("/")
        if len(parts) >= 3 and parts[-1] == signal and samples and workload(parts[1]) == pod:
            return {"pod": pod, "signal": signal,
                    "points": [{"ts": s["ts"], "value": s["value"]} for s in samples]}
    raise HTTPException(404, f"no '{signal}' series for workload '{pod}'")


@app.get("/api/events", tags=["telemetry"])
def events():
    """Recent anomaly_candidate events from L2 (the coarse threshold alert stream)."""
    try:
        return _get(AGG + "/events")
    except Exception as e:
        raise HTTPException(503, f"aggregator unreachable: {e}")


@app.get("/api/scenarios", tags=["scenarios"])
def scenarios():
    """Catalogue of fault scenarios and whether each can be fired through this API."""
    return SCENARIOS


@app.post("/api/scenarios/{sid}/trigger", tags=["scenarios"])
def trigger(sid: str):
    """Fire a scenario. S1 is wired (it arms cooling-monitor's fio storm over HTTP); the others
    are CronJob/flag based and trigger via scenarios/<id>/trigger.sh for now."""
    sid = sid.upper()
    if sid == "S1":
        try:
            _post(COOLING + "/flush")
            return {"scenario": "S1", "status": "armed"}
        except Exception as e:
            raise HTTPException(503, f"cooling-monitor unreachable: {e}")
    raise HTTPException(501, f"{sid} is not triggerable via the API yet — use scenarios/{sid}/trigger.sh")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
