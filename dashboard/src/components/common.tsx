import type { ReactNode } from "react";
import type { ActivityBucket, DecisionSummary } from "../types";
import { formatAge, formatScore, formatClock } from "../format";

export function Badge({ tone = "neutral", children, title }: {
  tone?: "neutral" | "ok" | "warn" | "bad" | "info" | "scoreless";
  children: ReactNode;
  title?: string;
}) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>;
}

export function Score({ value }: { value: number | null }) {
  if (value === null) return <span className="not-scored">Not scored</span>;
  return <span className="mono">{formatScore(value)}</span>;
}

export function ActionLabel({ action }: { action: DecisionSummary["action"] }) {
  return <span className={`action-pill ${action}`}>{action}</span>;
}

/** Loading / error / empty handling for a polled resource. Children render only with data. */
export function DataState<T>({ loading, error, data, empty, isEmpty, children }: {
  loading: boolean;
  error: string | null;
  data: T | null;
  empty: ReactNode;
  isEmpty: (data: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  if (data === null) {
    if (loading) return <div className="state" role="status">Loading…</div>;
    return (
      <div className="state" role="alert">
        Could not load data{error ? `: ${error}` : ""}. Retrying automatically.
      </div>
    );
  }
  if (isEmpty(data)) return <div className="state">{empty}</div>;
  return <>{children(data)}</>;
}

/** Shown when the latest refresh failed or the data is older than expected. */
export function Freshness({ updatedAt, stale, error }: {
  updatedAt: number | null;
  stale: boolean;
  error: string | null;
}) {
  if (updatedAt === null) return null;
  const age = (Date.now() - updatedAt) / 1000;
  if (!stale) return <span className="muted small">Updated {formatAge(age)}</span>;
  return (
    <Badge tone="warn" title={error ?? undefined}>
      Stale: last updated {formatAge(age)}{error ? " (refresh failing)" : ""}
    </Badge>
  );
}

/** Keyset pagination: the server returns a cursor for the next (older) page only, so earlier
 * cursors are kept on a stack to go back. */
export function Pager({ page, hasNext, onNext, onPrev, onFirst }: {
  page: number;
  hasNext: boolean;
  onNext: () => void;
  onPrev: () => void;
  onFirst: () => void;
}) {
  return (
    <nav className="pager" aria-label="Pagination">
      <span className="muted small">Page {page + 1}</span>
      <button className="btn" onClick={onFirst} disabled={page === 0}>Newest</button>
      <button className="btn" onClick={onPrev} disabled={page === 0}>Previous</button>
      <button className="btn" onClick={onNext} disabled={!hasNext}>Next</button>
    </nav>
  );
}

const SERIES = [
  { key: "approve", label: "Approved", color: "var(--approve-bar)" },
  { key: "review", label: "Scored review", color: "var(--review)" },
  { key: "scoreless", label: "Scoreless review", color: "var(--scoreless)" },
  { key: "decline", label: "Declined", color: "var(--decline)" },
] as const;

/** Stacked bars per time bucket. Scoreless reviews are split out of reviews. */
export function ActivityChart({ buckets, since, until, bucketSeconds }: {
  buckets: ActivityBucket[];
  since: string;
  until: string;
  bucketSeconds: number;
}) {
  const start = new Date(since).getTime();
  const end = new Date(until).getTime();
  const slots = Math.max(1, Math.ceil((end - start) / (bucketSeconds * 1000)));
  const width = 1000;
  const height = 120;
  const barW = width / slots;
  const max = Math.max(1, ...buckets.map((b) => b.total));
  return (
    <figure style={{ margin: 0 }}>
      <svg className="chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
        role="img" aria-label={`Decisions per ${bucketSeconds} s bucket`}>
        <line x1="0" x2={width} y1={height - 0.5} y2={height - 0.5} stroke="var(--border)" />
        {buckets.map((b) => {
          const x = ((new Date(b.bucket).getTime() - start) / (end - start)) * width;
          let y = height;
          const parts = {
            approve: b.approve,
            review: b.review - b.scoreless,
            scoreless: b.scoreless,
            decline: b.decline,
          };
          return (
            <g key={b.bucket}>
              <title>{`${formatClock(b.bucket)}: ${b.total} decisions (${b.approve} approved, ${b.review} reviews of which ${b.scoreless} scoreless, ${b.decline} declined)`}</title>
              {SERIES.map((s) => {
                const h = (parts[s.key] / max) * (height - 6);
                y -= h;
                return h > 0 ? (
                  <rect key={s.key} x={x + 0.5} y={y} width={Math.max(barW - 1, 1)} height={h} fill={s.color} />
                ) : null;
              })}
            </g>
          );
        })}
      </svg>
      <figcaption className="legend">
        {SERIES.map((s) => (
          <span key={s.key}><i style={{ background: s.color }} />{s.label}</span>
        ))}
        <span>Peak {max} per {bucketSeconds >= 60 ? `${bucketSeconds / 60} min` : `${bucketSeconds} s`}</span>
      </figcaption>
    </figure>
  );
}
