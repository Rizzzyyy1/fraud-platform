// Shapes returned by the console service (src/fraudplat/console). Timestamps are ISO strings.

export type Action = "approve" | "review" | "decline";
export type ReviewStatus = "open" | "in_review" | "closed";
export type Disposition = "suspected_fraud" | "likely_legitimate" | "needs_more_information";
export type RangeKey = "15m" | "1h" | "6h" | "24h";

export interface SessionInfo {
  analyst: string;
  role: "analyst" | "admin";
  csrf_token: string;
}

export interface DecisionSummary {
  transaction_id: string;
  decision_time: string;
  amount_minor: number;
  currency: string;
  score: number | null;
  action: Action;
  reason_codes: string[];
  degraded: boolean;
  scoreless: boolean;
  review_status: ReviewStatus | null;
}

export interface DecisionPage {
  items: DecisionSummary[];
  next_cursor: string | null;
  range: RangeKey;
  since: string;
  until: string;
}

export interface ActivityBucket {
  bucket: string;
  total: number;
  approve: number;
  review: number;
  decline: number;
  scoreless: number;
  degraded: number;
}

export interface Activity {
  range: RangeKey;
  bucket_seconds: number;
  since: string;
  until: string;
  buckets: ActivityBucket[];
  totals: Omit<ActivityBucket, "bucket">;
}

export interface QueueItem extends DecisionSummary {
  disposition: Disposition | null;
  last_analyst: string | null;
  reviewed_at: string | null;
}

export interface QueuePage {
  items: QueueItem[];
  counts: Record<ReviewStatus, number>;
  next_cursor: string | null;
}

export interface Review {
  id: number;
  status: ReviewStatus;
  disposition: Disposition | null;
  note: string | null;
  analyst: string;
  created_at: string;
}

export interface StoredDecision {
  transaction_id: string;
  hash_version: number;
  customer_id: string;
  terminal_id: string;
  amount_minor: number;
  currency: string;
  event_time: string;
  received_at: string;
  decision_time: string;
  persisted_at: string;
  score: number | null;
  action: Action;
  reason_codes: string[];
  features: Record<string, number | null>;
  feature_freshness: Record<string, unknown>;
  model_version: string | null;
  feature_version: string;
  policy_version: string;
  model_registry_ref: string | null;
  published_at: string | null;
  outbox_created_at: string;
}

export interface PolicyRule {
  version: string;
  review_threshold: number;
  decline_threshold: number | null;
  derived_for_model: string | null;
}

export interface DecisionDetail {
  decision: StoredDecision;
  reviews: Review[];
  policy?: PolicyRule | null;
}

export interface Snapshot<T> {
  state: "fresh" | "stale" | "unavailable";
  fetched_at: number | null;
  age_seconds: number | null;
  error: string | null;
  value: T | null;
}

export interface Readiness {
  http_status: number;
  status: "ready" | "degraded" | "not_ready";
  database: boolean;
  scorer: boolean;
  feature_store?: boolean;
  degraded: string[];
  model: {
    model_version: string | null;
    policy_version: string;
    uri?: string;
    registry_ref?: string;
    source?: string;
  };
  pipeline?: Record<string, unknown> & { reject: string | null; suppress_scoring?: string | null };
}

export interface RegistryTarget {
  uri: string;
  registry_ref: string;
  model_version: string | null;
  policy_version: string | null;
}

export interface LiveWindow {
  measured_at: number;
  window_seconds: number;
  requests: number;
  requests_per_second: number | null;
  responses_by_status: Record<string, number>;
  error_responses: number;
  error_rate: number | null;
  request_p95_le_ms: number | null;
  feature_read_timeouts: number;
}

export interface Health {
  now: number;
  deployment: { namespace: string; created_at: string; environment: string; data: string };
  running: Snapshot<Readiness>;
  registry_target: Snapshot<RegistryTarget>;
  live_window: LiveWindow | null;
  processes: Record<string, { pid: number | null; state: string }>;
  worker_hold: { job_id: number; until: number } | null;
  console: { database: boolean; telemetry_fetches: number; uptime_seconds: number };
}

export interface ResultRow {
  label: string;
  identity: string;
  metrics: Record<string, number | number[] | null | undefined>;
}

export interface ResultSet {
  key: string;
  title: string;
  split: "development" | "held-out";
  split_detail: string;
  source: string;
  rows: ResultRow[];
  note: string;
}

export interface ReleaseStatus {
  release: string;
  candidate: { model_version: string; policy_version: string; registry_ref: string | null; status: string };
  criteria: { name: string; passed: boolean }[];
  serving_diagnosis: Record<string, { passed: number; runs: number }>;
  limitation: string;
}

export interface Results {
  results: ResultSet[];
  release: ReleaseStatus | null;
  reports: { key: string; title: string }[];
}

export interface Job {
  id: number;
  kind: "traffic" | "failure_drill";
  params: { rate: number; duration_s: number };
  status: "running" | "succeeded" | "failed" | "cancelled";
  requested_by: string;
  start_offset: number;
  sent: number;
  outcomes: Record<string, number>;
  message: string | null;
  created_at: string;
  finished_at: string | null;
  hold: { job_id: number; until: number } | null;
}

export interface JobList {
  remaining_demo_transactions: number;
  jobs: Job[];
}
