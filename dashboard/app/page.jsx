"use client";
import { useEffect, useState } from "react";
import dynamic from "next/dynamic";

// React Flow touches `window`, so it must render client-only (never during static prerender).
const Graph = dynamic(() => import("./Graph"), { ssr: false });

// Each backend component is exposed on its own NodePort so it can be tested individually from the
// laptop before anything is embedded. Links resolve against whatever host you loaded this page from
// (Tailscale IP, LAN IP, …), so they work over any network.
const PORTS = { api: 30088, grafana: 30030, prometheus: 30090 };
const PSI = { uid: "skn-psi", slug: "skn-psi", panelId: 1 }; // provisioned PSI dashboard (deploy/grafana-psi-dashboard.yaml)

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}
const fmt = (x) => (typeof x === "number" ? x.toFixed(2) : x);

export default function Page() {
  const [graph, setGraph] = useState(null);
  const [narr, setNarr] = useState(null);
  const [health, setHealth] = useState(null);
  const [updated, setUpdated] = useState(null);
  const [host, setHost] = useState("");
  const [fired, setFired] = useState(null);

  useEffect(() => {
    setHost(window.location.hostname);
  }, []);

  async function fireS1() {
    setFired("firing…");
    try {
      const r = await fetch("/api/scenarios/S1/trigger", { method: "POST" });
      const j = await r.json().catch(() => ({}));
      setFired(r.ok ? `armed ${new Date().toLocaleTimeString()} — changes show in ~50s` : `error ${r.status}: ${j.detail || ""}`);
    } catch (e) {
      setFired("error: " + e);
    }
  }

  async function refresh() {
    try {
      const [g, n, h] = await Promise.all([
        getJSON("/api/graph"),
        getJSON("/api/narrative"),
        getJSON("/api/health"),
      ]);
      setGraph(g);
      setNarr(n);
      setHealth(h);
      setUpdated(new Date());
    } catch (e) {
      /* keep last good values */
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const link = (port, path = "") => (host ? `http://${host}:${port}${path}` : "#");
  const meta = graph?.meta || {};
  const root = graph?.root?.[0];
  const blast = graph?.blast_radius || [];

  const components = [
    { name: "Causal API", port: PORTS.api, path: "/docs", status: health ? (health.ok ? "up" : "degraded") : null,
      desc: "FastAPI gateway — /api/graph, /api/narrative, /api/pods. OpenAPI explorer at /docs." },
    { name: "Grafana", port: PORTS.grafana, path: "", status: null,
      desc: "Metric dashboards (PSI, CPU, memory) over Prometheus. Anonymous viewer; build/import panels." },
    { name: "Prometheus", port: PORTS.prometheus, path: "/graph", status: null,
      desc: "Raw PromQL + scrape targets. Try container_pressure_io_stalled_seconds_total." },
    { name: "Loki · logs", port: null, path: "", status: "pending",
      desc: "Log aggregation — pending the alloy → promtail fix, then added to Grafana." },
  ];

  return (
    <>
      <div className="topbar">
        <div className="title">▣ SiliconKnights · Causal AIOps</div>
        <div className="meta">
          <span className="pill">
            <span className="dot" style={{ background: health ? (health.ok ? "var(--green)" : "var(--red)") : "var(--text-faint)" }} />
            {health ? (health.ok ? "upstream up" : "upstream degraded") : "…"}
          </span>
          <span>signal: {meta.signal || "—"}</span>
          <span>↻ 5s · {updated ? updated.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }) : "…"} IST</span>
        </div>
      </div>

      <div className="wrap">
        {/* Component launcher — each opens on its own port for individual testing */}
        <div className="panel" style={{ gridColumn: "span 12" }}>
          <div className="head">
            Components <span className="sub">test each individually before we embed</span>
          </div>
          <div className="body" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 10 }}>
            {components.map((c) => (
              <div key={c.name} style={{ border: "0.5px solid var(--border)", borderRadius: 4, padding: 12, display: "flex", flexDirection: "column" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <b>{c.name}</b>
                  {c.status && (
                    <span className="dot" style={{ background: c.status === "up" ? "var(--green)" : c.status === "pending" ? "var(--text-faint)" : "var(--red)" }} />
                  )}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-weak)", margin: "6px 0 10px", flex: 1 }}>{c.desc}</div>
                {c.port ? (
                  <a className="btn" href={link(c.port, c.path)} target="_blank" rel="noreferrer" style={{ textDecoration: "none", alignSelf: "flex-start" }}>
                    open :{c.port} ↗
                  </a>
                ) : (
                  <span className="btn" style={{ opacity: 0.5, alignSelf: "flex-start" }}>pending</span>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* live causal verdict (served by this site, same origin via /api) */}
        <Stat label="Workloads" value={meta.pods ?? "—"} />
        <Stat label="Active" value={meta.active ?? 0} color={(meta.active ?? 0) > 0 ? "var(--orange)" : "var(--green)"} />
        <Stat label="Causal edges" value={meta.accepted_edges ?? graph?.edges?.length ?? "—"} />
        <Stat label="Root cause" value={root ? root.pod : "none"} color={root ? "var(--red)" : "var(--green)"} small />

        <Panel span={12} title="Verdict" sub={narr ? (narr.source === "llm" ? "gemma4" : "template fallback") : ""}>
          <div style={{ fontSize: 18, lineHeight: 1.5 }}>{narr?.text || "…"}</div>
          {root && (
            <div style={{ marginTop: 8, color: "var(--text-weak)" }}>
              root cause <b style={{ color: "var(--text)" }}>{root.pod}</b>
              {typeof root.score === "number" ? ` · score ${root.score.toFixed(2)}` : ""}
              {meta.case_register && <span className="chip" style={{ marginLeft: 8 }}>{meta.case_register}</span>}
            </div>
          )}
        </Panel>

        <Panel span={12} title="Causal graph" sub="edge width ∝ confidence · hot = live incident · grey = steady backbone" bodyStyle={{ height: 460, padding: 0, flex: "none" }}>
          <Graph graph={graph} />
        </Panel>

        <Panel span={12} title="PSI / I/O pressure" sub="Grafana · Prometheus" bodyStyle={{ padding: 0, flex: "none" }}>
          {host && (
            <iframe
              title="PSI panel"
              src={`http://${host}:${PORTS.grafana}/d-solo/${PSI.uid}/${PSI.slug}?orgId=1&panelId=${PSI.panelId}&theme=dark&from=now-30m&to=now&refresh=5s&timezone=Asia/Kolkata`}
              style={{ width: "100%", height: 460, border: 0, display: "block" }}
            />
          )}
        </Panel>

        <Panel span={6} title="Blast radius">
          {blast.length ? (
            blast.map((b) => (
              <div key={b.pod} className="row">
                <span>{b.pod}</span>
                <span style={{ color: "var(--text-weak)" }}>impact {fmt(b.impact)} · eta ~{b.eta_s}s</span>
              </div>
            ))
          ) : (
            <div style={{ color: "var(--text-faint)" }}>no predicted victims</div>
          )}
        </Panel>

        <Panel span={6} title="Scenarios">
          <button className="btn btn-primary" onClick={fireS1}>▶ Fire S1</button>
          {fired && <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-weak)" }}>{fired}</div>}
          <div style={{ marginTop: 10, color: "var(--text-faint)", fontSize: 11 }}>S2–S5 fire via scenarios/&lt;id&gt;/trigger.sh</div>
        </Panel>
      </div>
    </>
  );
}

function Stat({ label, value, color, small }) {
  return (
    <div className="panel stat" style={{ gridColumn: "span 3" }}>
      <div className="v" style={{ color: color || "var(--text)", fontSize: small ? 18 : 30 }}>{value}</div>
      <div className="l">{label}</div>
    </div>
  );
}

function Panel({ span, title, sub, children, bodyStyle }) {
  return (
    <div className="panel" style={{ gridColumn: `span ${span}` }}>
      <div className="head">
        {title}
        {sub ? <span className="sub">{sub}</span> : null}
      </div>
      <div className="body" style={bodyStyle}>{children}</div>
    </div>
  );
}
