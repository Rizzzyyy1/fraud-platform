import { useState } from "react";
import { api } from "../api";
import { usePolling } from "../usePolling";
import type { Health as HealthData, ResultSet, Results, Snapshot } from "../types";
import { Badge, DataState, Freshness } from "../components/common";
import { formatAge, formatNumber, formatPercent } from "../format";

function SnapshotState({ snap, label }: { snap: Snapshot<unknown>; label: string }) {
  if (snap.state === "fresh") return <Badge tone="ok">{label}: fetched {formatAge(snap.age_seconds)}</Badge>;
  if (snap.state === "stale")
    return <Badge tone="warn" title={snap.error ?? undefined}>{label}: stale, last good {formatAge(snap.age_seconds)}{snap.error ? ` (${snap.error})` : ""}</Badge>;
  return <Badge tone="bad" title={snap.error ?? undefined}>{label}: unavailable{snap.error ? ` (${snap.error})` : ""}</Badge>;
}

function Identity({ health }: { health: HealthData }) {
  const running = health.running.value?.model;
  const target = health.registry_target.value;
  const same = running?.registry_ref && target?.registry_ref && running.registry_ref === target.registry_ref;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Model identity</h2>
        <span className="spacer" />
        <SnapshotState snap={health.running} label="Running API" />
        <SnapshotState snap={health.registry_target} label="Registry" />
      </div>
      <div className="grid two">
        <div>
          <h3>Running deployment</h3>
          <p className="muted small" style={{ margin: "2px 0 8px" }}>
            Resolved once when the API process started; recorded on every decision.
          </p>
          {running ? (
            <dl className="kv">
              <dt>Model</dt><dd className="identity">{running.model_version ?? "none"}</dd>
              <dt>Policy</dt><dd className="identity">{running.policy_version}</dd>
              <dt>Registry version</dt><dd className="identity">{running.registry_ref ?? "not from registry"}</dd>
              <dt>Resolved from</dt><dd className="identity">{running.uri ?? "file path"} ({running.source ?? "file"})</dd>
            </dl>
          ) : <p className="muted">Unavailable: the scoring API has not answered.</p>}
        </div>
        <div>
          <h3>Registry alias target</h3>
          <p className="muted small" style={{ margin: "2px 0 8px" }}>
            Where the alias points now. A change takes effect only after the API restarts.
          </p>
          {target ? (
            <dl className="kv">
              <dt>Model</dt><dd className="identity">{target.model_version ?? "untagged"}</dd>
              <dt>Policy</dt><dd className="identity">{target.policy_version ?? "untagged"}</dd>
              <dt>Registry version</dt><dd className="identity">{target.registry_ref}</dd>
              <dt>Alias</dt><dd className="identity">{target.uri}</dd>
            </dl>
          ) : <p className="muted">Unavailable: the registry could not be read.</p>}
        </div>
      </div>
      <div style={{ marginTop: 10 }}>
        {running && target ? (
          same ? <Badge tone="ok">Running deployment matches the alias target</Badge>
            : <Badge tone="warn">Alias target differs from the running deployment: restart required to apply it</Badge>
        ) : <Badge>Comparison unavailable</Badge>}
      </div>
    </div>
  );
}

function Pipeline({ health }: { health: HealthData }) {
  const r = health.running.value;
  const p = r?.pipeline;
  const live = health.live_window;
  const num = (v: unknown, suffix = "") => (v === null || v === undefined ? "unknown" : `${formatNumber(Number(v), 1)}${suffix}`);
  // Ages near zero can round below zero across clocks; never show a negative age.
  const age = (v: unknown) => (v === null || v === undefined ? "unknown" : `${Math.max(0, Number(v)).toFixed(1)} s`);
  const rows = (v: unknown) => (v === null || v === undefined ? "unknown" : `${Number(v).toLocaleString()} ${Number(v) === 1 ? "row" : "rows"}`);
  return (
    <div className="grid two">
      <div className="panel">
        <div className="panel-head"><h2>Live serving (this deployment)</h2></div>
        <p className="muted small" style={{ marginTop: 0 }}>
          From the running API's counters over the last minute. Local, single machine, demo traffic
          rates; not comparable with the benchmark results.
        </p>
        {live ? (
          <div className="stats">
            <div className="stat"><div className="label">Requests</div><div className="value">{live.requests.toLocaleString()}</div><div className="hint">in {Math.round(live.window_seconds)} s</div></div>
            <div className="stat"><div className="label">Throughput</div><div className="value">{live.requests_per_second?.toFixed(1) ?? "—"}/s</div></div>
            <div className="stat"><div className="label">Error responses</div><div className="value">{live.error_responses}</div><div className="hint">{live.error_rate === null ? "no requests" : formatPercent(live.error_rate, 2)}</div></div>
            <div className="stat"><div className="label">Request p95</div><div className="value">{live.request_p95_le_ms === null ? "—" : `≤ ${live.request_p95_le_ms} ms`}</div><div className="hint">histogram bucket bound</div></div>
            <div className="stat"><div className="label">Feature read timeouts</div><div className="value">{live.feature_read_timeouts}</div></div>
          </div>
        ) : (
          <p className="muted">No measurement window yet: needs two scrapes of the API within a minute.</p>
        )}
      </div>
      <div className="panel">
        <div className="panel-head">
          <h2>Pipeline</h2>
          <span className="spacer" />
          {r ? (r.status === "ready" ? <Badge tone="ok">Ready</Badge> : r.status === "degraded" ? <Badge tone="warn">Degraded</Badge> : <Badge tone="bad">Not ready</Badge>) : <Badge tone="bad">Unknown</Badge>}
        </div>
        {p?.suppress_scoring && (
          <div className="banner bad" role="alert">
            Scoring suppressed ({p.suppress_scoring}): new decisions are persisted as scoreless reviews.
          </div>
        )}
        {r && r.degraded.length > 0 && <div className="banner warn">Degraded signals: {r.degraded.join(", ")}</div>}
        {p ? (
          <dl className="kv">
            <dt>Outbox backlog</dt><dd>{rows(p.outbox_backlog)}</dd>
            <dt>Oldest unpublished</dt><dd>{p.oldest_unpublished_age_s === null ? "none" : age(p.oldest_unpublished_age_s)}</dd>
            <dt>Worker heartbeat age</dt><dd>{age(p.worker_heartbeat_age_s)}</dd>
            <dt>Consumer lag</dt><dd>{p.consumer_lag_events === -1 ? "unknown" : num(p.consumer_lag_events, " events")}</dd>
            <dt>Unknown state for</dt><dd>{p.unknown_state_for_s === null ? "—" : num(p.unknown_state_for_s, " s")}</dd>
            <dt>Scoring suppressed</dt><dd>{p.suppress_scoring ?? "no"}</dd>
            <dt>Rejecting new decisions</dt><dd>{p.reject ?? "no"}</dd>
            <dt>Feature store</dt><dd>{r?.feature_store === false ? "unreachable" : "reachable"}</dd>
          </dl>
        ) : <p className="muted">Pipeline status unavailable.</p>}
        <div className="divider" />
        <h3>Processes</h3>
        <dl className="kv" style={{ marginTop: 6 }}>
          {Object.entries(health.processes).map(([name, proc]) => (
            <Proc key={name} name={name} state={proc.state} pid={proc.pid} />
          ))}
        </dl>
      </div>
    </div>
  );
}

function Proc({ name, state, pid }: { name: string; state: string; pid: number | null }) {
  const tone = state === "running" ? "ok" : state.startsWith("stopped") ? "warn" : "bad";
  return (
    <>
      <dt>{name}</dt>
      <dd><Badge tone={tone}>{state}</Badge> {pid && <span className="muted small mono">pid {pid}</span>}</dd>
    </>
  );
}

const METRIC_LABELS: Record<string, string> = {
  mean_average_precision: "Mean AP (folds)",
  average_precision: "AP",
  average_precision_95ci: "AP 95% CI",
  reviewed: "Reviewed",
  declined: "Declined",
  flagged_precision: "Flagged precision",
  flagged_recall: "Flagged recall",
  legitimate_reviewed: "Legitimate reviewed",
  review_rate: "Review rate",
};

function metric(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return `${Number(value[0]).toFixed(3)}–${Number(value[1]).toFixed(3)}`;
  if (key === "review_rate") return formatPercent(Number(value), 2);
  return formatNumber(Number(value));
}

function ResultTable({ set }: { set: ResultSet }) {
  const keys = Array.from(new Set(set.rows.flatMap((r) => Object.keys(r.metrics))));
  return (
    <div className="panel">
      <div className="panel-head">
        <h2>{set.title}</h2>
        <Badge tone={set.split === "held-out" ? "info" : "neutral"}>{set.split === "held-out" ? "Held-out test" : "Development"}</Badge>
        <span className="muted small">{set.split_detail}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th></th><th>Artifact + policy</th>{keys.map((k) => <th key={k} className="num">{METRIC_LABELS[k] ?? k}</th>)}</tr></thead>
          <tbody>
            {set.rows.map((r) => (
              <tr key={r.label}>
                <td>{r.label}</td>
                <td className="identity">{r.identity}</td>
                {keys.map((k) => <td key={k} className="num">{metric(k, r.metrics[k])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small" style={{ marginBottom: 0 }}>{set.note} Source: <code>{set.source}</code></p>
    </div>
  );
}

function ReportViewer({ reports }: { reports: Results["reports"] }) {
  const [open, setOpen] = useState<string | null>(null);
  const [text, setText] = useState<{ title: string; markdown: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const show = async (key: string) => {
    setOpen(key);
    setText(null);
    setError(null);
    try {
      setText(await api.get(`/api/reports/${key}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <div className="panel report">
      <div className="panel-head"><h2>Reports</h2><span className="muted small">Committed report files, shown as written.</span></div>
      <div className="toolbar">
        {reports.map((r) => (
          <button key={r.key} className="btn" aria-pressed={open === r.key} onClick={() => show(r.key)}>{r.title}</button>
        ))}
        {open && <button className="btn" onClick={() => { setOpen(null); setText(null); }}>Hide</button>}
      </div>
      {open && !text && !error && <div className="state">Loading…</div>}
      {error && <div className="banner bad">{error}</div>}
      {text && <pre aria-label={text.title}>{text.markdown}</pre>}
    </div>
  );
}

export function Health() {
  const health = usePolling<HealthData>((signal) => api.get("/api/health", signal), 10_000, []);
  const results = usePolling<Results>((signal) => api.get("/api/results", signal), 300_000, []);
  return (
    <>
      <div className="toolbar" style={{ justifyContent: "flex-end" }}>
        <Freshness updatedAt={health.updatedAt} stale={health.stale} error={health.error} />
      </div>
      <DataState loading={health.loading} error={health.error} data={health.data} isEmpty={() => false} empty={null}>
        {(h) => (
          <>
            {h.running.state !== "fresh" && (
              <div className="banner bad" role="alert">
                Scoring API telemetry is {h.running.state}
                {h.running.fetched_at ? `; values below are from ${formatAge(h.running.age_seconds)}` : ""}.
              </div>
            )}
            <Identity health={h} />
            <Pipeline health={h} />
          </>
        )}
      </DataState>
      <DataState loading={results.loading} error={results.error} data={results.data} isEmpty={() => false} empty={null}>
        {(res) => (
          <>
            {res.release && (
              <div className="panel">
                <div className="panel-head">
                  <h2>Release candidate</h2>
                  <Badge tone="warn">{res.release.candidate.status}</Badge>
                  <span className="identity">{res.release.candidate.model_version} + {res.release.candidate.policy_version}</span>
                  {res.release.candidate.registry_ref && <span className="muted small">({res.release.candidate.registry_ref})</span>}
                </div>
                <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
                  {res.release.criteria.map((c) => (
                    <li key={c.name}>{c.passed ? <Badge tone="ok">pass</Badge> : <Badge tone="bad">fail</Badge>} {c.name}</li>
                  ))}
                </ul>
                <div className="banner warn">
                  <strong>Unresolved limitation.</strong> {res.release.limitation} Serving diagnosis at 100 rps:{" "}
                  {Object.entries(res.release.serving_diagnosis).map(([m, t]) => `${m} ${t.passed}/${t.runs} runs passed`).join("; ")}.
                </div>
              </div>
            )}
            {res.results.map((set) => <ResultTable key={set.key} set={set} />)}
            <ReportViewer reports={res.reports} />
          </>
        )}
      </DataState>
    </>
  );
}
