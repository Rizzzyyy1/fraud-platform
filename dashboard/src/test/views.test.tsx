import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { formatScore } from "../format";
import { DataState, Freshness, Score } from "../components/common";
import { DecisionDetail, ruleText } from "../views/DecisionDetail";
import { Health } from "../views/Health";
import { Live } from "../views/Live";
import { App } from "../App";
import { setCsrfToken } from "../api";
import { usePolling } from "../usePolling";
import type { DecisionDetail as Detail, Health as HealthData } from "../types";

type Handler = (url: string, init?: RequestInit) => { status?: number; body: unknown };

function mockFetch(handler: Handler) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const { status = 200, body } = handler(url, init);
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(null);
});

const decision: Detail = {
  decision: {
    transaction_id: "D1-TX1",
    hash_version: 1,
    customer_id: "C00001",
    terminal_id: "T00001",
    amount_minor: 12345,
    currency: "USD",
    event_time: "2025-05-20T10:00:00Z",
    received_at: "2026-09-25T14:00:00.100Z",
    decision_time: "2026-09-25T14:00:00.105Z",
    persisted_at: "2026-09-25T14:00:00.160Z",
    score: null,
    action: "review",
    reason_codes: ["PIPELINE_STALE_BEYOND_LIMIT", "WORKER_HEARTBEAT_STALE", "NO_SCORE"],
    features: {},
    feature_freshness: {},
    model_version: null,
    feature_version: "f1",
    policy_version: "pol-6164cb21826d",
    model_registry_ref: "fraud-risk-f1/5",
    published_at: "2026-09-25T14:00:00.300Z",
    outbox_created_at: "2026-09-25T14:00:00.160Z",
  },
  reviews: [],
};

function health(overrides: Partial<HealthData> = {}): HealthData {
  return {
    now: 1,
    deployment: { namespace: "replay:dashboard-1", created_at: "x", environment: "local", data: "simulated" },
    running: {
      state: "fresh",
      fetched_at: 1,
      age_seconds: 1,
      error: null,
      value: {
        http_status: 200,
        status: "ready",
        database: true,
        scorer: true,
        feature_store: true,
        degraded: [],
        model: {
          model_version: "lr-f1-6f0ebad8fcc7",
          policy_version: "pol-6164cb21826d",
          uri: "models:/fraud-risk-f1@production",
          registry_ref: "fraud-risk-f1/5",
          source: "registry",
        },
        pipeline: { reject: null, suppress_scoring: null, outbox_backlog: 0, consumer_lag_events: 0 },
      },
    },
    registry_target: {
      state: "fresh",
      fetched_at: 1,
      age_seconds: 1,
      error: null,
      value: {
        uri: "models:/fraud-risk-f1@production",
        registry_ref: "fraud-risk-f1/5",
        model_version: "lr-f1-6f0ebad8fcc7",
        policy_version: "pol-6164cb21826d",
      },
    },
    live_window: null,
    processes: { api: { pid: 1, state: "running" }, worker: { pid: null, state: "stopped by failure drill" } },
    worker_hold: null,
    console: { database: true, telemetry_fetches: 1, uptime_seconds: 1 },
    ...overrides,
  };
}

describe("scores", () => {
  it("never shows a missing score as a number", () => {
    expect(formatScore(null)).toBe("Not scored");
    expect(formatScore(0)).toBe("0.000");
    render(<Score value={null} />);
    expect(screen.getByText("Not scored")).toBeInTheDocument();
  });
});

describe("data states", () => {
  const props = { isEmpty: (d: number[]) => d.length === 0, empty: "Nothing here" };
  it("renders loading, error, empty and content", () => {
    const { rerender } = render(<DataState loading error={null} data={null} {...props}>{() => "content"}</DataState>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    rerender(<DataState loading={false} error="HTTP 500" data={null} {...props}>{() => "content"}</DataState>);
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 500");
    rerender(<DataState loading={false} error={null} data={[]} {...props}>{() => "content"}</DataState>);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    rerender(<DataState loading={false} error={null} data={[1]} {...props}>{() => "content"}</DataState>);
    expect(screen.getByText("content")).toBeInTheDocument();
  });
  it("marks stale data instead of hiding it", () => {
    render(<Freshness updatedAt={Date.now() - 120_000} stale error="Console service unreachable" />);
    expect(screen.getByText(/Stale: last updated 2 min ago/)).toBeInTheDocument();
  });
});

describe("decision detail", () => {
  beforeEach(() => setCsrfToken("csrf-123"));

  it("shows the stored record read-only, explains reason codes, and appends a review", async () => {
    let reviews: Detail["reviews"] = [];
    const calls = mockFetch((_url, init) => {
      if (init?.method === "POST") {
        reviews = [{ id: 1, status: "in_review", disposition: null, note: "looking", analyst: "ana", created_at: "2026-09-25T14:05:00Z" }];
        return { status: 201, body: reviews[0] };
      }
      return { body: { ...decision, reviews } };
    });
    render(<DecisionDetail transactionId="D1-TX1" />);
    await screen.findByText("From transaction to auditable decision");
    expect(screen.getAllByText("Not scored").length).toBeGreaterThan(0);
    expect(screen.getByText("Model not called")).toBeInTheDocument();
    expect(screen.getByText(/not a\s+per-feature explanation/)).toBeInTheDocument();
    expect(screen.getByText(/not a confirmed fraud label/)).toBeInTheDocument();
    expect(screen.getByText("fraud-risk-f1/5")).toBeInTheDocument();
    expect(screen.getByText(/feature store was not read/)).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Analyst note/), { target: { value: "looking" } });
    fireEvent.click(screen.getByText("Save review"));
    await screen.findByText("Review saved.");
    const post = calls.find((c) => c.init?.method === "POST");
    expect(post?.url).toBe("/api/decisions/D1-TX1/reviews");
    expect((post?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-123");
    expect(JSON.parse(String(post?.init?.body))).toEqual({ status: "in_review", disposition: null, note: "looking" });
    expect(calls.every((c) => !c.init?.method || c.init.method === "GET" || c.init.method === "POST")).toBe(true);
    await screen.findByText("looking");
  });

  it("requires a disposition before closing", async () => {
    mockFetch(() => ({ body: decision }));
    render(<DecisionDetail transactionId="D1-TX1" />);
    await screen.findByText("Save review");
    fireEvent.change(screen.getByLabelText("New review status"), { target: { value: "closed" } });
    expect(screen.getByText("Save review")).toBeDisabled();
    expect(screen.getByText("Closing requires a disposition.")).toBeInTheDocument();
  });
});

describe("health", () => {
  const results = { results: [], release: null, reports: [] };
  it("separates the running deployment from the registry target", async () => {
    const h = health();
    h.registry_target.value = { ...h.registry_target.value!, registry_ref: "fraud-risk-f1/6", model_version: "xgb-x" };
    mockFetch((url) => ({ body: url === "/api/health" ? h : results }));
    render(<Health />);
    await screen.findByText(/Alias target differs from the running deployment: restart required/);
    expect(screen.getByText("fraud-risk-f1/5")).toBeInTheDocument();
    expect(screen.getByText("fraud-risk-f1/6")).toBeInTheDocument();
    expect(screen.getByText("stopped by failure drill")).toBeInTheDocument();
  });
  it("reports unavailable telemetry explicitly", async () => {
    const h = health({
      running: { state: "unavailable", fetched_at: null, age_seconds: null, error: "ConnectError", value: null },
    });
    mockFetch((url) => ({ body: url === "/api/health" ? h : results }));
    render(<Health />);
    await screen.findByText(/Scoring API telemetry is unavailable/);
    expect(screen.getByText(/Running API: unavailable \(ConnectError\)/)).toBeInTheDocument();
    expect(screen.getByText("Pipeline status unavailable.")).toBeInTheDocument();
  });
  it("labels held-out and development results with their identities", async () => {
    mockFetch((url) => ({
      body: url === "/api/health" ? health() : {
        reports: [],
        release: null,
        results: [
          { key: "held_out", title: "Release-1 held-out evaluation (run once)", split: "held-out", split_detail: "days", source: "x.json", note: "n", rows: [{ label: "Candidate", identity: "xgb-a + pol-b", metrics: { average_precision: 0.357 } }] },
          { key: "active_policy", title: "Active review-only policy", split: "development", split_detail: "d", source: "y.json", note: "No held-out result exists for this policy.", rows: [{ label: "Days 123-130", identity: "lr + pol-c", metrics: { review_rate: 0.0102 } }] },
        ],
      },
    }));
    render(<Health />);
    await screen.findByText("Held-out test");
    expect(screen.getByText("Development")).toBeInTheDocument();
    expect(screen.getByText("xgb-a + pol-b")).toBeInTheDocument();
    expect(screen.getByText(/No held-out result exists for this policy/)).toBeInTheDocument();
    expect(screen.getByText("1.02%")).toBeInTheDocument();
  });
  it("labels the real-data benchmark as offline and not deployed", async () => {
    mockFetch((url) => ({
      body: url === "/api/health" ? health() : {
        reports: [],
        release: null,
        results: [{ key: "ulb_benchmark", title: "Real-data offline benchmark (ULB)", split: "external", split_detail: "held out: elapsed hours 32–48", source: "reports/external/ulb/benchmark.json", note: "These models do not power the live scoring service.", rows: [{ label: "XGBoost", identity: "offline fit, not saved: max_depth=3", metrics: { validation_ap: 0.828, average_precision: 0.746 } }] }],
      },
    }));
    render(<Health />);
    await screen.findByText("Real data · offline only · not deployed");
    expect(screen.getByText(/do not power the live scoring service/)).toBeInTheDocument();
    expect(screen.getByText("Validation AP")).toBeInTheDocument();
    expect(screen.getByText("offline fit, not saved: max_depth=3")).toBeInTheDocument();
  });
});

describe("live activity", () => {
  it("shows scoreless decisions as Not scored and the pipeline state", async () => {
    const h = health();
    h.running.value!.status = "degraded";
    h.running.value!.degraded = ["WORKER_HEARTBEAT_STALE"];
    h.running.value!.pipeline!.suppress_scoring = "PIPELINE_STALE_BEYOND_LIMIT";
    mockFetch((url) => {
      if (url.startsWith("/api/decisions"))
        return { body: { items: [{ transaction_id: "D1-TX1", decision_time: "2026-09-25T14:00:00Z", amount_minor: 500, currency: "USD", score: null, action: "review", reason_codes: ["NO_SCORE"], degraded: true, scoreless: true, review_status: "open" }], next_cursor: "c2", range: "15m", since: "", until: "" } };
      if (url.startsWith("/api/activity"))
        return { body: { range: "15m", bucket_seconds: 30, since: "2026-09-25T13:45:00Z", until: "2026-09-25T14:00:00Z", buckets: [], totals: { total: 0, approve: 0, review: 0, decline: 0, scoreless: 0, degraded: 0 } } };
      return { body: h };
    });
    render(<Live onOpen={() => {}} isAdmin={false} />);
    await screen.findByText("D1-TX1");
    expect(screen.getAllByText("Not scored").length).toBeGreaterThan(0);
    expect(screen.getByText("Scoreless")).toBeInTheDocument();
    await screen.findByText(/Scoring suppressed: PIPELINE_STALE_BEYOND_LIMIT/);
    expect(screen.getByText(/No decisions in this range/)).toBeInTheDocument();
    expect(screen.getByText("Next")).toBeEnabled();
    expect(screen.queryByText("Demo traffic")).not.toBeInTheDocument(); // admin only
  });
});

describe("policy rule statement", () => {
  const reviewOnly = { version: "pol-a", review_threshold: 0.0381, decline_threshold: null, derived_for_model: "m" };
  const withDecline = { ...reviewOnly, decline_threshold: 0.0745 };
  const d = (score: number | null, codes: string[] = []) => ({ ...decision.decision, score, reason_codes: codes });
  it("states the threshold comparison, never a feature explanation", () => {
    expect(ruleText(d(0.005), reviewOnly)).toBe("Score 0.0050 < review threshold 0.0381 → approve.");
    expect(ruleText(d(0.2), reviewOnly)).toBe("Score 0.2000 ≥ review threshold 0.0381 → review.");
    expect(ruleText(d(0.2), withDecline)).toBe("Score 0.2000 ≥ decline threshold 0.0745 → decline.");
    expect(ruleText(d(null, ["PIPELINE_STALE_BEYOND_LIMIT", "NO_SCORE"]), reviewOnly)).toContain(
      "not called (PIPELINE_STALE_BEYOND_LIMIT)",
    );
    expect(ruleText(d(0.2), null)).toContain("not available locally");
  });
});

describe("pipeline badge", () => {
  it("does not present a stale health poll as current", async () => {
    const { PipelineBadge } = await import("../views/Live");
    render(<PipelineBadge health={health()} pollStale />);
    expect(screen.getByText(/Pipeline status stale/)).toBeInTheDocument();
    expect(screen.queryByText(/Pipeline healthy/)).not.toBeInTheDocument();
  });
});

describe("app shell", () => {
  it("labels the data as simulated and the deployment as local after sign-in", async () => {
    mockFetch((url) => {
      if (url === "/api/session") return { body: { analyst: "ana", role: "analyst", csrf_token: "t" } };
      return { status: 503, body: { detail: "down" } };
    });
    await act(async () => { render(<App />); });
    await waitFor(() => expect(screen.getByText("Simulated data")).toBeInTheDocument());
    expect(screen.getByText("Local deployment")).toBeInTheDocument();
  });
  it("shows the sign-in form without a session", async () => {
    mockFetch(() => ({ status: 401, body: { detail: "sign in required" } }));
    await act(async () => { render(<App />); });
    expect(await screen.findByText("Sign in")).toBeInTheDocument();
  });
});

describe("polling across query changes", () => {
  const renders: string[] = [];
  function Probe({ page, fetcher }: { page: number; fetcher: (p: number) => Promise<string> }) {
    const state = usePolling(() => fetcher(page), 60_000, [page]);
    renders.push(`page ${page}: ${state.data ?? "none"}`);
    return <div data-testid="probe">{state.data ?? "loading"}</div>;
  }
  it("never renders the previous query's data under the new query", async () => {
    const pending: Record<number, (v: string) => void> = {};
    const fetcher = (p: number) => new Promise<string>((resolve) => { pending[p] = resolve; });
    const { rerender } = render(<Probe page={1} fetcher={fetcher} />);
    await act(async () => { pending[1]?.("rows of page 1"); });
    expect(screen.getByTestId("probe")).toHaveTextContent("rows of page 1");
    rerender(<Probe page={2} fetcher={fetcher} />);
    await act(async () => { pending[2]?.("rows of page 2"); });
    expect(screen.getByTestId("probe")).toHaveTextContent("rows of page 2");
    // Every render is recorded, including the one before effects run.
    expect(renders).not.toContain("page 2: rows of page 1");
  });
});
