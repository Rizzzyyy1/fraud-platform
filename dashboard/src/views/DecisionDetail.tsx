import { useState } from "react";
import { api } from "../api";
import { usePolling } from "../usePolling";
import type { DecisionDetail as Detail, Disposition, PolicyRule, ReviewStatus, StoredDecision } from "../types";
import { Badge, DataState, Score } from "../components/common";
import {
  DISPOSITION_LABELS,
  REASON_CODES,
  STATUS_LABELS,
  formatAmount,
  formatTime,
  formatTimeMs,
  msBetween,
  readableFeature,
} from "../format";

function formatFeature(value: number | null): string {
  if (value === null) return "missing";
  if (Number.isInteger(value)) return value.toLocaleString("en-US");
  return value.toFixed(4);
}

/** States the stored policy rule that produced the action. It compares the score with the
 * policy's thresholds; it says nothing about which features drove the score. */
export function ruleText(d: StoredDecision, policy: PolicyRule | null | undefined): string {
  if (d.score === null) {
    const cause = d.reason_codes.find((c) => c !== "NO_SCORE" && c !== "PIPELINE_DEGRADED");
    return `The model was not called${cause ? ` (${cause})` : ""}; without a score the decision is recorded as a review.`;
  }
  if (!policy) return `Policy ${d.policy_version} file is not available locally; see the stored reason codes.`;
  const s = d.score.toFixed(4);
  if (policy.decline_threshold !== null && d.score >= policy.decline_threshold)
    return `Score ${s} ≥ decline threshold ${policy.decline_threshold.toFixed(4)} → decline.`;
  if (d.score >= policy.review_threshold)
    return `Score ${s} ≥ review threshold ${policy.review_threshold.toFixed(4)} → review.`;
  return `Score ${s} < review threshold ${policy.review_threshold.toFixed(4)} → approve.`;
}

function Lifecycle({ d }: { d: StoredDecision }) {
  const decideMs = msBetween(d.received_at, d.decision_time);
  const publishMs = msBetween(d.outbox_created_at, d.published_at);
  const steps = [
    { name: "Transaction", when: d.event_time, note: "simulated event time", done: true },
    { name: "Received", when: d.received_at, note: "API clock", done: true },
    { name: "Decided", when: d.decision_time, note: decideMs !== null ? `+${decideMs} ms after receipt (API clock)` : "", done: true },
    { name: "Persisted with outbox event", when: d.persisted_at, note: "database clock; one transaction", done: true },
    {
      name: "Event published to Kafka",
      when: d.published_at,
      note: publishMs !== null ? `+${publishMs} ms after the outbox row (database clock)` : "not yet published",
      done: d.published_at !== null,
    },
  ];
  return (
    <div className="lifecycle" aria-label="Decision lifecycle">
      {steps.map((st) => (
        <div key={st.name} className={`step${st.done ? " done" : ""}`}>
          <div className="name">{st.name}</div>
          <div className="when">{st.when ? formatTimeMs(st.when) : "—"}</div>
          <div className={st.name.startsWith("Decided") || st.name.startsWith("Event") ? "delta" : "muted small"}>{st.note}</div>
        </div>
      ))}
    </div>
  );
}

function ReviewForm({ transactionId, current, onSaved }: {
  transactionId: string;
  current: ReviewStatus;
  onSaved: () => void;
}) {
  const [status, setStatus] = useState<ReviewStatus>(current === "open" ? "in_review" : current);
  const [disposition, setDisposition] = useState<Disposition | "">("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const needsDisposition = status === "closed" && disposition === "";

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await api.post(`/api/decisions/${encodeURIComponent(transactionId)}/reviews`, {
        status,
        disposition: disposition === "" ? null : disposition,
        note: note.trim() === "" ? null : note.trim(),
      });
      setNote("");
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label="Add review">
      <div className="toolbar">
        <label>Status
          <select aria-label="New review status" value={status} onChange={(e) => setStatus(e.target.value as ReviewStatus)}>
            {(["open", "in_review", "closed"] as const).map((s) => <option key={s} value={s}>{STATUS_LABELS[s]}</option>)}
          </select>
        </label>
        <label>Disposition
          <select aria-label="Disposition" value={disposition} onChange={(e) => setDisposition(e.target.value as Disposition | "")}>
            <option value="">None</option>
            {Object.entries(DISPOSITION_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      <label className="sr-only" htmlFor="review-note">Note</label>
      <textarea id="review-note" placeholder="Analyst note (optional, up to 2,000 characters)" maxLength={2000}
        value={note} onChange={(e) => setNote(e.target.value)} />
      <div className="toolbar" style={{ marginTop: 8 }}>
        <button className="btn primary" type="submit" disabled={saving || needsDisposition}>
          {saving ? "Saving…" : "Save review"}
        </button>
        {needsDisposition && <span className="muted small">Closing requires a disposition.</span>}
        {saved && <span className="small" role="status" style={{ color: "var(--ok)" }}>Review saved.</span>}
      </div>
      {error && <div className="banner bad" role="alert">{error}</div>}
    </form>
  );
}

export function DecisionDetail({ transactionId, onClose, onReviewed }: {
  transactionId: string;
  onClose?: () => void;
  onReviewed?: () => void;
}) {
  const detail = usePolling<Detail>(
    (signal) => api.get(`/api/decisions/${encodeURIComponent(transactionId)}`, signal),
    30_000,
    [transactionId],
  );
  return (
    <div className="panel" aria-label="Decision detail">
      <div className="panel-head">
        <h2 className="mono">{transactionId}</h2>
        <span className="spacer" />
        {onClose && <button className="btn" onClick={onClose}>Close</button>}
      </div>
      <DataState loading={detail.loading} error={detail.error} data={detail.data} isEmpty={() => false} empty={null}>
        {({ decision: d, reviews, policy }) => {
          const latest = reviews[reviews.length - 1];
          const status: ReviewStatus = latest?.status ?? "open";
          return (
            <>
              <div className="summary" aria-label="Decision summary">
                <span className={`big ${d.action}`}>{d.action}</span>
                <span className="rule">
                  Risk score <Score value={d.score} /> ·{" "}
                  {ruleText(d, policy)}{" "}
                  {d.action === "review" && <Badge tone="info">Review: {STATUS_LABELS[status]}</Badge>}{" "}
                  {d.score === null && <Badge tone="scoreless">Model not called</Badge>}{" "}
                  {d.reason_codes.includes("PIPELINE_DEGRADED") && (
                    <Badge tone="warn">{d.score === null ? "Pipeline degraded" : "Scored while pipeline degraded"}</Badge>
                  )}
                </span>
                <span className="caveat">
                  {d.score === null
                    ? "No model score exists for this decision; the review is a safety default, not a risk judgment."
                    : "The rule compares the model's score with the policy's thresholds. It does not say which features drove the score."}
                </span>
              </div>

              <h3>From transaction to auditable decision</h3>
              <Lifecycle d={d} />

              <div className="grid two section">
                <div>
                  <h3>Transaction (simulated)</h3>
                  <dl className="kv" style={{ marginTop: 6 }}>
                    <dt>Amount</dt><dd>{formatAmount(d.amount_minor, d.currency)}</dd>
                    <dt>Customer</dt><dd className="mono">{d.customer_id}</dd>
                    <dt>Terminal</dt><dd className="mono">{d.terminal_id}</dd>
                  </dl>
                </div>
                <div>
                  <h3>Decided by (stored with the decision)</h3>
                  <dl className="kv" style={{ marginTop: 6 }}>
                    <dt>Model</dt><dd className="identity">{d.model_version ?? "none (not scored)"}</dd>
                    <dt>Policy</dt><dd className="identity">{d.policy_version}{policy ? (policy.decline_threshold === null ? " · review-only" : " · with automatic declines") : ""}</dd>
                    <dt>Registry version</dt><dd className="identity">{d.model_registry_ref ?? "not recorded"}</dd>
                    <dt>Feature version</dt><dd className="identity">{d.feature_version}</dd>
                  </dl>
                </div>
              </div>

              <div className="divider" />
              <h3>Why it was routed this way: stored reason codes</h3>
              <p className="muted small" style={{ margin: "4px 0 8px" }}>
                Policy thresholds and data-quality flags recorded with the decision. They are not a
                per-feature explanation of the model's score.
              </p>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {d.reason_codes.map((c) => (
                  <li key={c}><code>{c}</code> <span className="muted">— {REASON_CODES[c] ?? "undocumented code"}</span></li>
                ))}
              </ul>

              <div className="divider" />
              <h3>Stored feature values (as read at decision time)</h3>
              <p className="muted small" style={{ margin: "4px 0 8px" }}>Exactly what the model received. Amounts are stored in minor units (cents).</p>
              {Object.keys(d.features).length === 0 ? (
                <p className="muted">No feature values were stored: the feature store was not read for this decision.</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>Feature</th><th className="num">Stored value</th><th>Reads as</th></tr></thead>
                    <tbody>
                      {Object.entries(d.features).map(([k, v]) => (
                        <tr key={k}>
                          <td className="mono">{k}</td>
                          <td className="num mono">{formatFeature(v)}</td>
                          <td>{readableFeature(k, v)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {Object.keys(d.feature_freshness).length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary className="muted small">Feature freshness record</summary>
                  <dl className="kv" style={{ marginTop: 6 }}>
                    {Object.entries(d.feature_freshness).map(([k, v]) => (
                      <FragmentKV key={k} k={k} v={v} />
                    ))}
                  </dl>
                </details>
              )}

              <div className="divider" />
              <h3>Analyst review (stored separately)</h3>
              <p className="muted small" style={{ margin: "4px 0 8px" }}>
                Reviews are appended separately and never change the decision above. A disposition is
                an analyst judgment, not a confirmed fraud label, and is not used for training.
                Confirmed labels are not connected to this console.
              </p>
              {reviews.length === 0 ? (
                <p className="muted">No reviews yet.</p>
              ) : (
                <div className="review-log">
                  {reviews.map((r) => (
                    <div className="entry" key={r.id}>
                      <strong>{STATUS_LABELS[r.status]}</strong>
                      {r.disposition && <> · {DISPOSITION_LABELS[r.disposition]}</>}
                      <span className="muted small"> — {r.analyst}, {formatTime(r.created_at)}</span>
                      {r.note && <div style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{r.note}</div>}
                    </div>
                  ))}
                </div>
              )}
              <div style={{ marginTop: 10 }}>
                <ReviewForm transactionId={d.transaction_id} current={status}
                  onSaved={() => { detail.refresh(); onReviewed?.(); }} />
              </div>
            </>
          );
        }}
      </DataState>
    </div>
  );
}

function FragmentKV({ k, v }: { k: string; v: unknown }) {
  return (
    <>
      <dt className="mono">{k}</dt>
      <dd className="mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
    </>
  );
}
