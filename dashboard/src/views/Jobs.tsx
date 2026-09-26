import { useState } from "react";
import { api } from "../api";
import { usePolling } from "../usePolling";
import type { Job, JobList } from "../types";
import { Badge, DataState } from "../components/common";
import { formatTime } from "../format";

const RATES = [2, 5, 10, 20];
const DURATIONS = [30, 60, 180, 300];

function statusTone(status: Job["status"]) {
  return status === "succeeded" ? "ok" : status === "running" ? "info" : status === "failed" ? "bad" : "neutral";
}

/** Predefined demo jobs only: kind, rate and duration are chosen from fixed lists. */
export function Jobs() {
  const [kind, setKind] = useState<"traffic" | "failure_drill">("traffic");
  const [rate, setRate] = useState(5);
  const [duration, setDuration] = useState(60);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const jobs = usePolling<JobList>((signal) => api.get("/api/jobs", signal), 3000, []);
  const running = jobs.data?.jobs.find((j) => j.status === "running");

  const start = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api.post("/api/jobs", { kind, rate, duration_s: kind === "traffic" ? duration : null });
      jobs.refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const cancel = async (id: number) => {
    setBusy(true);
    try {
      await api.post(`/api/jobs/${id}/cancel`);
      jobs.refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Demo traffic</h2>
        <span className="muted small">
          Pre-test simulated transactions (days 140–152) replayed into this local deployment. One job at a time.
        </span>
        <span className="spacer" />
        {jobs.data && (
          <span className="muted small">{jobs.data.remaining_demo_transactions.toLocaleString()} demo transactions left</span>
        )}
      </div>
      <div className="toolbar">
        <label>Job
          <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} disabled={busy}>
            <option value="traffic">Traffic</option>
            <option value="failure_drill">Failure drill (worker stopped 90 s)</option>
          </select>
        </label>
        <label>Rate
          <select value={rate} onChange={(e) => setRate(Number(e.target.value))} disabled={busy}>
            {RATES.filter((r) => kind === "traffic" || r <= 10).map((r) => <option key={r} value={r}>{r}/s</option>)}
          </select>
        </label>
        {kind === "traffic" ? (
          <label>Duration
            <select value={duration} onChange={(e) => setDuration(Number(e.target.value))} disabled={busy}>
              {DURATIONS.map((d) => <option key={d} value={d}>{d} s</option>)}
            </select>
          </label>
        ) : (
          <span className="muted small">150 s; worker stopped from 20 s to 110 s, then restarted</span>
        )}
        <button className="btn primary" onClick={start} disabled={busy || running !== undefined}>Start</button>
        {running && (
          <button className="btn danger" onClick={() => cancel(running.id)} disabled={busy}>Cancel job {running.id}</button>
        )}
      </div>
      {message && <div className="banner bad" role="alert">{message}</div>}
      <DataState loading={jobs.loading} error={jobs.error} data={jobs.data}
        isEmpty={(d) => d.jobs.length === 0} empty="No jobs have run in this deployment.">
        {(d) => (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Job</th><th>Kind</th><th>Parameters</th><th>Status</th><th className="num">Sent</th>
                  <th>Outcomes</th><th>Started (UTC)</th><th>Notes</th></tr>
              </thead>
              <tbody>
                {d.jobs.slice(0, 5).map((j) => (
                  <tr key={j.id}>
                    <td className="mono">{j.id}</td>
                    <td>{j.kind === "traffic" ? "Traffic" : "Failure drill"}</td>
                    <td className="mono">{j.params.rate}/s · {j.params.duration_s} s</td>
                    <td><Badge tone={statusTone(j.status)}>{j.status}</Badge>{j.hold && <> <Badge tone="warn">worker stopped</Badge></>}</td>
                    <td className="num">{j.sent.toLocaleString()}</td>
                    <td className="small mono">{Object.entries(j.outcomes).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</td>
                    <td className="mono small">{formatTime(j.created_at)}</td>
                    <td className="small">{j.message ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataState>
    </div>
  );
}
