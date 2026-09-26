import { useState } from "react";
import { api, query } from "../api";
import { usePolling } from "../usePolling";
import type { Activity, DecisionPage, Health, RangeKey } from "../types";
import { ActionLabel, ActivityChart, Badge, DataState, Freshness, Pager, Score } from "../components/common";
import { formatAmount, formatPercent, formatTime, RANGE_LABELS, STATUS_LABELS } from "../format";
import { Jobs } from "./Jobs";

const POLL_MS = 5000;

export function PipelineBadge({ health, pollStale = false }: { health: Health | null; pollStale?: boolean }) {
  if (!health) return <Badge>Pipeline status unknown</Badge>;
  if (pollStale) return <Badge tone="warn">Pipeline status stale: console not answering</Badge>;
  const running = health.running;
  if (running.state === "unavailable") return <Badge tone="bad">Scoring API unreachable</Badge>;
  const r = running.value;
  if (!r) return <Badge>Pipeline status unknown</Badge>;
  const stale = running.state === "stale" ? " (stale)" : "";
  if (r.status === "not_ready") return <Badge tone="bad">Not ready{stale}</Badge>;
  if (r.pipeline?.suppress_scoring)
    return <Badge tone="bad" title={r.degraded.join(", ")}>Scoring suppressed: {r.pipeline.suppress_scoring}{stale}</Badge>;
  if (r.status === "degraded")
    return <Badge tone="warn" title={r.degraded.join(", ")}>Degraded: {r.degraded.join(", ")}{stale}</Badge>;
  return <Badge tone="ok">Pipeline healthy{stale}</Badge>;
}

export function Live({ onOpen, isAdmin }: { onOpen: (id: string) => void; isAdmin: boolean }) {
  const [range, setRange] = useState<RangeKey>("15m");
  const [action, setAction] = useState("");
  const [scoring, setScoring] = useState("");
  const [degraded, setDegraded] = useState("");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1] ?? null;
  const filters = [range, action, scoring, degraded];

  const page = usePolling<DecisionPage>(
    (signal) =>
      api.get(`/api/decisions${query({ range, action, scoring, degraded, cursor, limit: 25 })}`, signal),
    POLL_MS,
    [...filters, cursor],
  );
  const activity = usePolling<Activity>(
    (signal) => api.get(`/api/activity${query({ range })}`, signal),
    POLL_MS * 2,
    [range],
  );
  const health = usePolling<Health>((signal) => api.get("/api/health", signal), POLL_MS * 2, []);

  const resetPaging = () => setCursors([null]);
  const totals = activity.data?.totals;
  const live = health.stale ? null : health.data?.live_window;
  const telemetryHint = health.stale ? "stale: console not answering" : null;

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2>Activity</h2>
          <PipelineBadge health={health.data} pollStale={health.stale} />
          <span className="spacer" />
          <label className="muted small">
            Time range{" "}
            <select value={range} onChange={(e) => { setRange(e.target.value as RangeKey); resetPaging(); }}>
              {Object.entries(RANGE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <Freshness updatedAt={activity.updatedAt} stale={activity.stale} error={activity.error} />
        </div>
        <div className="stats">
          <div className="stat">
            <div className="label">Decisions ({RANGE_LABELS[range]?.toLowerCase()})</div>
            <div className="value">{totals ? totals.total.toLocaleString() : "—"}</div>
          </div>
          <div className="stat">
            <div className="label">Reviews</div>
            <div className="value">{totals ? totals.review.toLocaleString() : "—"}</div>
            <div className="hint">{totals && totals.total ? formatPercent(totals.review / totals.total, 2) : ""}</div>
          </div>
          <div className="stat">
            <div className="label">Scoreless reviews</div>
            <div className="value">{totals ? totals.scoreless.toLocaleString() : "—"}</div>
            <div className="hint">model not called</div>
          </div>
          <div className="stat">
            <div className="label">Degraded decisions</div>
            <div className="value">{totals ? totals.degraded.toLocaleString() : "—"}</div>
          </div>
          <div className="stat">
            <div className="label">Throughput (API)</div>
            <div className="value">{live && live.requests_per_second !== null ? `${live.requests_per_second.toFixed(1)}/s` : "—"}</div>
            <div className="hint">{telemetryHint ?? (live ? `last ${Math.round(live.window_seconds)} s` : "no telemetry window yet")}</div>
          </div>
          <div className="stat">
            <div className="label">API errors</div>
            <div className="value">{live ? live.error_responses.toLocaleString() : "—"}</div>
            <div className="hint">{telemetryHint ?? (live ? (live.error_rate === null ? "no requests in window" : formatPercent(live.error_rate, 2)) : "")}</div>
          </div>
        </div>
        <div className="divider" />
        <DataState
          loading={activity.loading}
          error={activity.error}
          data={activity.data}
          isEmpty={(a) => a.buckets.length === 0}
          empty={<>No decisions in this range. Start demo traffic{isAdmin ? " below" : " (admin)"}.</>}
        >
          {(a) => <ActivityChart buckets={a.buckets} since={a.since} until={a.until} bucketSeconds={a.bucket_seconds} />}
        </DataState>
      </div>

      {isAdmin && <Jobs />}

      <div className="panel">
        <div className="panel-head">
          <h2>Recent decisions</h2>
          <span className="spacer" />
          <Freshness updatedAt={page.updatedAt} stale={page.stale} error={page.error} />
        </div>
        <div className="toolbar" role="group" aria-label="Filters">
          <label>Action
            <select value={action} onChange={(e) => { setAction(e.target.value); resetPaging(); }}>
              <option value="">All</option><option value="approve">Approve</option>
              <option value="review">Review</option><option value="decline">Decline</option>
            </select>
          </label>
          <label>Score
            <select value={scoring} onChange={(e) => { setScoring(e.target.value); resetPaging(); }}>
              <option value="">All</option><option value="scored">Scored</option>
              <option value="scoreless">Not scored</option>
            </select>
          </label>
          <label>Pipeline
            <select value={degraded} onChange={(e) => { setDegraded(e.target.value); resetPaging(); }}>
              <option value="">All</option><option value="true">Degraded only</option>
              <option value="false">Healthy only</option>
            </select>
          </label>
          {cursors.length > 1 && <span className="muted small">Viewing older pages: auto-refresh keeps this page.</span>}
        </div>
        <DataState
          loading={page.loading}
          error={page.error}
          data={page.data}
          isEmpty={(p) => p.items.length === 0}
          empty={<>No decisions match these filters in the {RANGE_LABELS[range]?.toLowerCase()}.</>}
        >
          {(p) => (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Decision time (UTC)</th><th>Transaction</th><th className="num">Amount</th>
                      <th className="num">Risk score</th><th>Action</th><th>Pipeline</th><th>Review</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.items.map((d) => (
                      <tr key={d.transaction_id} className="clickable" tabIndex={0}
                        onClick={() => onOpen(d.transaction_id)}
                        onKeyDown={(e) => { if (e.key === "Enter") onOpen(d.transaction_id); }}>
                        <td className="mono">{formatTime(d.decision_time)}</td>
                        <td className="mono">{d.transaction_id}</td>
                        <td className="num">{formatAmount(d.amount_minor, d.currency)}</td>
                        <td className="num"><Score value={d.score} /></td>
                        <td><ActionLabel action={d.action} /></td>
                        <td>
                          {d.scoreless ? <Badge tone="scoreless">Scoreless</Badge>
                            : d.degraded ? <Badge tone="warn">Degraded</Badge>
                            : <span className="muted small">Healthy</span>}
                        </td>
                        <td>{d.review_status ? STATUS_LABELS[d.review_status] : <span className="muted">—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pager page={cursors.length - 1} hasNext={p.next_cursor !== null}
                onNext={() => setCursors((c) => [...c, p.next_cursor])}
                onPrev={() => setCursors((c) => c.slice(0, -1))}
                onFirst={resetPaging} />
            </>
          )}
        </DataState>
      </div>
    </>
  );
}
