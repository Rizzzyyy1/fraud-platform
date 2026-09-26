// Display helpers. A missing score is never shown as a number.

export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return "Not scored";
  return score.toFixed(3);
}

export function formatAmount(minor: number, currency: string): string {
  const value = minor / 100;
  return `${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function formatClock(iso: string): string {
  const d = new Date(iso);
  return d.toISOString().slice(11, 19) + "Z";
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatNumber(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined) return "—";
  if (Number.isInteger(value)) return value.toLocaleString("en-US");
  return value.toFixed(digits);
}

export const RANGE_LABELS: Record<string, string> = {
  "15m": "Last 15 minutes",
  "1h": "Last hour",
  "6h": "Last 6 hours",
  "24h": "Last 24 hours",
};

export const DISPOSITION_LABELS: Record<string, string> = {
  suspected_fraud: "Suspected fraud",
  likely_legitimate: "Likely legitimate",
  needs_more_information: "Needs more information",
};

export const STATUS_LABELS: Record<string, string> = {
  open: "Open",
  in_review: "In review",
  closed: "Closed",
};

// Reason codes are policy thresholds and data-quality flags, not per-feature model explanations.
export const REASON_CODES: Record<string, string> = {
  SCORE_BELOW_REVIEW_THRESHOLD: "Score below the policy's review threshold",
  SCORE_AT_OR_ABOVE_REVIEW_THRESHOLD: "Score at or above the policy's review threshold",
  SCORE_AT_OR_ABOVE_DECLINE_THRESHOLD: "Score at or above the policy's decline threshold",
  COLD_START_CUSTOMER: "Customer has no 30-day history (scored from imputed values)",
  HISTORY_POSSIBLY_TRIMMED: "Retention may have removed part of the feature window",
  PIPELINE_DEGRADED: "The event pipeline was degraded when this decision was made",
  WORKER_HEARTBEAT_STALE: "Feature worker heartbeat older than 10 s",
  WORKER_HEARTBEAT_MISSING: "No feature worker heartbeat",
  OUTBOX_BACKLOG_AGED: "Oldest unpublished event older than 30 s",
  CONSUMER_LAG_HIGH: "Consumer lag above 1,000 events",
  CONSUMER_LAG_UNKNOWN: "Consumer lag unknown",
  PIPELINE_STALE_BEYOND_LIMIT: "Pipeline stale beyond 60 s: model not called",
  PIPELINE_UNKNOWN_BEYOND_LIMIT: "Pipeline state unknown beyond 60 s: model not called",
  FEATURES_UNAVAILABLE: "Feature store unreachable or read timed out: model not called",
  NO_SCORE: "No model score for this decision",
  SCAFFOLD_NOT_A_MODEL: "Placeholder scorer, not a model",
};

export function formatTimeMs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ");
}

export function msBetween(from: string | null | undefined, to: string | null | undefined): number | null {
  if (!from || !to) return null;
  return new Date(to).getTime() - new Date(from).getTime();
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} days`;
}

// Readable renderings of stored feature values; the raw stored value is always shown too.
const MONEY_FEATURES = new Set(["amount_minor", "cust_amount_mean_7d", "cust_amount_mean_30d"]);
const FLAG_FEATURES = new Set(["cust_has_history_30d", "cust_terminal_is_new_30d"]);

export function readableFeature(name: string, value: number | null): string {
  if (value === null) return "missing";
  if (MONEY_FEATURES.has(name)) return formatAmount(value, "USD");
  if (FLAG_FEATURES.has(name)) return value ? "yes" : "no";
  if (name === "secs_since_prev_cust_txn") return formatDuration(value);
  if (name === "amount_to_cust_mean_30d") return `${value.toFixed(2)}× the 30-day mean`;
  if (name === "hour_of_day_utc") return `${String(value).padStart(2, "0")}:00 UTC`;
  if (name === "day_of_week_utc") return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][value] ?? String(value);
  return value.toLocaleString("en-US");
}
