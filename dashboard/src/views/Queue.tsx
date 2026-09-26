import { useState } from "react";
import { api, query } from "../api";
import { usePolling } from "../usePolling";
import type { QueuePage, ReviewStatus } from "../types";
import { Badge, DataState, Freshness, Pager, Score } from "../components/common";
import { DISPOSITION_LABELS, STATUS_LABELS, formatAmount, formatTime } from "../format";
import { DecisionDetail } from "./DecisionDetail";

export function Queue({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const [status, setStatus] = useState<ReviewStatus>("open");
  const [scoring, setScoring] = useState("");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1] ?? null;
  const queue = usePolling<QueuePage>(
    (signal) => api.get(`/api/queue${query({ status, scoring, cursor, limit: 20 })}`, signal),
    15_000,
    [status, scoring, cursor],
  );
  const reset = () => setCursors([null]);

  return (
    <div className="split">
      <div className="panel">
        <div className="panel-head">
          <h2>Review queue</h2>
          <span className="spacer" />
          <Freshness updatedAt={queue.updatedAt} stale={queue.stale} error={queue.error} />
        </div>
        <div className="tabs" role="tablist" aria-label="Review status" style={{ marginBottom: 10 }}>
          {(["open", "in_review", "closed"] as const).map((s) => (
            <button key={s} role="tab" className="tab" aria-selected={status === s}
              onClick={() => { setStatus(s); reset(); }}>
              {STATUS_LABELS[s]}{queue.data ? ` (${queue.data.counts[s].toLocaleString()})` : ""}
            </button>
          ))}
        </div>
        <div className="toolbar">
          <label>Score
            <select value={scoring} onChange={(e) => { setScoring(e.target.value); reset(); }}>
              <option value="">All</option><option value="scored">Scored</option>
              <option value="scoreless">Not scored</option>
            </select>
          </label>
          <span className="muted small">Priority: model score, highest first; scoreless reviews after scored ones.</span>
        </div>
        <DataState loading={queue.loading} error={queue.error} data={queue.data}
          isEmpty={(q) => q.items.length === 0}
          empty={<>No {STATUS_LABELS[status]?.toLowerCase()} reviews.</>}>
          {(q) => (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th className="num">Score</th><th>Transaction</th><th className="num">Amount</th>
                      <th>Decided (UTC)</th><th>Status</th></tr>
                  </thead>
                  <tbody>
                    {q.items.map((item) => (
                      <tr key={item.transaction_id} tabIndex={0}
                        className={`clickable${item.transaction_id === selected ? " selected" : ""}`}
                        onClick={() => onSelect(item.transaction_id)}
                        onKeyDown={(e) => { if (e.key === "Enter") onSelect(item.transaction_id); }}>
                        <td className="num"><Score value={item.score} /></td>
                        <td className="mono small">{item.transaction_id}</td>
                        <td className="num">{formatAmount(item.amount_minor, item.currency)}</td>
                        <td className="mono small">{formatTime(item.decision_time)}</td>
                        <td>
                          {item.scoreless && <Badge tone="scoreless">Scoreless</Badge>}{" "}
                          {item.disposition ? DISPOSITION_LABELS[item.disposition] : STATUS_LABELS[item.review_status ?? "open"]}
                          {item.last_analyst && <div className="muted small">{item.last_analyst}</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pager page={cursors.length - 1} hasNext={q.next_cursor !== null}
                onNext={() => setCursors((c) => [...c, q.next_cursor])}
                onPrev={() => setCursors((c) => c.slice(0, -1))} onFirst={reset} />
            </>
          )}
        </DataState>
      </div>
      {selected ? (
        <DecisionDetail transactionId={selected} onClose={() => onSelect(null)} onReviewed={queue.refresh} />
      ) : (
        <div className="panel state">Select a decision to see its stored record and review history.</div>
      )}
    </div>
  );
}
