// End-to-end walkthrough of the running local dashboard in the installed Google Chrome
// (playwright-core; no browser download). Every check is an assertion: on failure the script
// saves evidence (screenshot, page HTML, the observed values) and exits non-zero.
//
// Covers: anonymous access rejected; session-cookie protections; demo traffic; live-activity and
// investigation pagination; loading, empty, error and stale states; a review stored separately
// with the decision unchanged; model and system health; the failure drill (degraded, then
// scoring suppressed, then recovered); logout invalidating the session.
//
// Usage (admin account; credentials from the environment only):
//   E2E_USER=... E2E_PASSWORD=... node e2e/walkthrough.mjs
// Writes screenshots to docs/screenshots/, results to reports/dashboard/walkthrough.json, and on
// failure evidence to run/e2e-evidence/<timestamp>/ (git-ignored).

import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.CONSOLE_URL ?? "http://127.0.0.1:8200";
const API = process.env.API_URL ?? "http://127.0.0.1:8110";
const USER = process.env.E2E_USER;
const PASSWORD = process.env.E2E_PASSWORD;
const ROOT = new URL("../../", import.meta.url).pathname;
const SHOTS = `${ROOT}docs/screenshots/`;
const OUT = `${ROOT}reports/dashboard/walkthrough.json`;
const EVIDENCE_REL = `run/e2e-evidence/${new Date().toISOString().replace(/[:.]/g, "-")}/`;
const EVIDENCE = `${ROOT}${EVIDENCE_REL}`; // results record only the repository-relative path
if (!USER || !PASSWORD) throw new Error("set E2E_USER and E2E_PASSWORD");
mkdirSync(SHOTS, { recursive: true });

const results = { started_at: new Date().toISOString(), base_url: BASE, checks: [], steps: [] };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const note = (name, detail = {}) => {
  results.steps.push({ name, at: new Date().toISOString(), ...detail });
  console.log(`- ${name}`, Object.keys(detail).length ? JSON.stringify(detail) : "");
};

class CheckFailed extends Error {}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

/** Assert `ok`; on failure keep evidence and stop. `observed` is recorded either way. */
async function check(name, ok, observed) {
  results.checks.push({ name, passed: Boolean(ok), observed });
  console.log(`${ok ? "✓" : "✗"} ${name}`, JSON.stringify(observed));
  if (ok) return;
  mkdirSync(EVIDENCE, { recursive: true });
  await page.screenshot({ path: `${EVIDENCE}page.png`, fullPage: true }).catch(() => {});
  writeFileSync(`${EVIDENCE}page.html`, await page.content().catch(() => ""));
  writeFileSync(`${EVIDENCE}failed-check.json`, JSON.stringify({ name, observed, url: page.url() }, null, 2));
  results.evidence_dir = EVIDENCE_REL;
  throw new CheckFailed(`check failed: ${name} (evidence in ${EVIDENCE_REL})`);
}
const shot = async (name, opts = {}) => {
  await page.screenshot({ path: `${SHOTS}${name}.png`, ...opts });
  note(`screenshot ${name}`);
};
const api = (path, init) =>
  page.evaluate(async ([p, i]) => {
    const r = await fetch(p, { credentials: "same-origin", ...i });
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    return { status: r.status, body };
  }, [path, init]);
const readyz = async () => (await fetch(`${API}/readyz`)).json();
const pollUntil = async (fn, timeoutMs, stepMs = 2000) => {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await fn();
    if (last.ok) return last;
    await sleep(stepMs);
  }
  return last;
};

try {
  // 1. Anonymous access and sign-in.
  await page.goto(BASE);
  await page.getByRole("button", { name: "Sign in" }).waitFor();
  const anon = await Promise.all(["/api/decisions", "/api/queue", "/api/health", "/api/jobs"].map((p) => api(p)));
  await check("anonymous API reads are rejected with 401", anon.every((r) => r.status === 401), anon.map((r) => r.status));
  await shot("01-sign-in");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByText("Simulated data", { exact: true }).waitFor();
  const cookie = (await context.cookies()).find((c) => c.name === "fp_console");
  await check("session cookie is HttpOnly, SameSite=Strict, path /", cookie?.httpOnly === true && cookie?.sameSite === "Strict" && cookie?.path === "/",
    { httpOnly: cookie?.httpOnly, sameSite: cookie?.sameSite, path: cookie?.path });
  const scriptVisible = await page.evaluate(() => document.cookie.includes("fp_console"));
  await check("session cookie is not readable from page scripts", scriptVisible === false, { visible_to_script: scriptVisible });
  const bundle = await page.evaluate(async () => {
    const scripts = [...document.querySelectorAll("script[src]")].map((s) => s.src);
    const texts = await Promise.all(scripts.map((s) => fetch(s).then((r) => r.text())));
    return { scripts: scripts.length, mentions_api_key_header: texts.some((t) => t.includes("X-API-Key")) };
  });
  await check("dashboard bundle contains no API-key header", bundle.mentions_api_key_header === false, bundle);

  // 2. Live activity with demo traffic.
  const jobs0 = await api("/api/jobs");
  if (!jobs0.body.jobs.some((j) => j.status === "running")) {
    await page.getByLabel("Job").selectOption("traffic");
    await page.getByLabel("Rate").selectOption("10");
    await page.getByLabel("Duration").selectOption("180");
    await page.getByRole("button", { name: "Start" }).click();
  }
  const started = await pollUntil(async () => {
    const j = await api("/api/jobs");
    return { ok: j.body.jobs.some((x) => x.status === "running"), jobs: j.body.jobs.slice(0, 1) };
  }, 15000, 1000);
  await check("traffic job running", started.ok, started.jobs);
  await sleep(25_000);
  await page.getByLabel("Time range").selectOption("1h");
  await sleep(6_000);
  await shot("02-live-activity", { fullPage: true });

  // Empty state: this release never declines.
  await page.getByLabel("Action").selectOption("decline");
  await page.getByText(/No decisions match these filters/).waitFor();
  await check("empty state shown for a filter with no rows", true, { filter: "action=decline" });
  await shot("13-empty-state");
  await page.getByLabel("Action").selectOption("");

  // Scoreless filter (from earlier drills, if any in range).
  await page.getByLabel("Score").selectOption("scoreless");
  await sleep(2_000);
  await shot("03-live-scoreless-filter");
  await page.getByLabel("Score").selectOption("");

  // 3. Investigation: a review stored separately.
  await page.getByRole("tab", { name: /Investigation/ }).click();
  await page.getByRole("tab", { name: /^Open/ }).waitFor();
  await sleep(1_500);
  const firstRow = page.locator("tbody tr.clickable").first();
  const txn = (await firstRow.locator("td").nth(1).innerText()).trim();
  await firstRow.click();
  await page.getByText("From transaction to auditable decision").waitFor();
  const before = await api(`/api/decisions/${encodeURIComponent(txn)}`);
  await shot("04-investigation-detail", { fullPage: true });
  await page.getByLabel("New review status").selectOption("in_review");
  await page.getByPlaceholder(/Analyst note/).fill("Amount well above this customer's 30-day average; checking terminal history.");
  await page.getByRole("button", { name: "Save review" }).click();
  await page.getByText("Review saved.").waitFor();
  await page.getByLabel("New review status").selectOption("closed");
  await page.getByLabel("Disposition").selectOption("needs_more_information");
  await page.getByPlaceholder(/Analyst note/).fill("Closed pending customer contact (demo).");
  await page.getByRole("button", { name: "Save review" }).click();
  await page.getByText("Review saved.").waitFor();
  await sleep(1_000);
  const after = await api(`/api/decisions/${encodeURIComponent(txn)}`);
  const statuses = after.body.reviews.map((r) => r.status).slice(-2);
  await check("both reviews persisted", JSON.stringify(statuses) === JSON.stringify(["in_review", "closed"]), { transaction_id: txn, statuses });
  await check("original decision unchanged by the reviews", JSON.stringify(before.body.decision) === JSON.stringify(after.body.decision),
    { identities: { model: after.body.decision.model_version, policy: after.body.decision.policy_version, registry: after.body.decision.model_registry_ref } });
  await page.getByText("Closed pending customer contact (demo).").scrollIntoViewIfNeeded();
  await shot("05-review-recorded");
  await page.getByRole("tab", { name: /^Closed/ }).click();
  await sleep(1_500);
  await shot("06-queue-closed");

  // Loading and error states for the queue (injected in the browser; the service is unchanged).
  // Route handlers may still be pending when the route is removed; ignore "already handled".
  await page.route("**/api/queue**", async (route) => { await sleep(3000); await route.continue().catch(() => {}); });
  await page.getByRole("tab", { name: /^In review/ }).click();
  await page.getByRole("status").filter({ hasText: "Loading" }).first().waitFor();
  await check("loading state shown while the queue loads", true, { delay_ms: 3000 });
  await shot("14-loading-state");
  await page.unroute("**/api/queue**");
  await page.route("**/api/queue**", (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"injected failure"}' }).catch(() => {}));
  await page.getByRole("tab", { name: /^Open/ }).click();
  await page.getByText(/Could not load data: injected failure/).waitFor({ timeout: 15000 });
  await check("error state shown when the queue request fails", true, { injected_status: 500 });
  await shot("15-error-state");
  await page.unroute("**/api/queue**");

  // 4. Model and system health, and a committed report.
  await page.getByRole("tab", { name: /Model & system health/ }).click();
  await page.getByRole("heading", { name: "Running deployment" }).waitFor();
  await sleep(2_000);
  await shot("07-health", { fullPage: true });
  const health = await api("/api/health");
  await check("running model matches the alias target", health.body.running.value?.model?.registry_ref === health.body.registry_target.value?.registry_ref,
    { running: health.body.running.value?.model, target: health.body.registry_target.value });
  await page.getByRole("button", { name: "Release-1 held-out evaluation" }).click();
  await page.locator("pre").waitFor();
  await page.locator("pre").scrollIntoViewIfNeeded();
  await shot("08-report-held-out");

  // 5. Polling overhead while idle on the live view.
  await page.getByRole("tab", { name: /Live activity/ }).click();
  await sleep(3_000);
  await page.evaluate(() => performance.clearResourceTimings());
  const idleSeconds = 60;
  await sleep(idleSeconds * 1000);
  const polling = await page.evaluate(() =>
    performance.getEntriesByType("resource").filter((e) => e.name.includes("/api/"))
      .map((e) => ({ path: new URL(e.name).pathname, ms: e.duration })));
  const byPath = {};
  for (const p of polling) {
    byPath[p.path] ??= { requests: 0, total_ms: 0 };
    byPath[p.path].requests += 1;
    byPath[p.path].total_ms += p.ms;
  }
  for (const v of Object.values(byPath)) { v.mean_ms = Math.round((v.total_ms / v.requests) * 10) / 10; delete v.total_ms; }
  const perMinute = Math.round((polling.length / idleSeconds) * 60);
  await check("polling stays bounded (at most 60 console requests per minute per tab)", perMinute <= 60, { requests_per_minute: perMinute, by_path: byPath });

  // 6. Console unreachable from the browser: data kept and marked stale.
  await page.route("**/api/**", (route) => route.abort().catch(() => {}));
  await sleep(25_000);
  await shot("09-stale-console-unreachable");
  const staleBadges = await page.getByText(/^Stale: last updated/).count();
  const staleStatus = await page.getByText("Pipeline status stale: console not answering").count();
  await check("stale state shown while the console is unreachable", staleBadges >= 2 && staleStatus === 1, { stale_badges: staleBadges, pipeline_badge_stale: staleStatus });
  await page.unroute("**/api/**");
  await sleep(12_000);

  // 7. Controlled failure and recovery.
  const idle = await pollUntil(async () => {
    const j = await api("/api/jobs");
    return { ok: !j.body.jobs.some((x) => x.status === "running") };
  }, 300_000, 5000);
  await check("previous job finished before the drill", idle.ok, {});

  // Pagination, checked while no demo job is writing (so pages cannot shift underneath).
  await page.getByLabel("Time range").selectOption("1h");
  await sleep(6_000);
  const firstRows = await page.locator("tbody tr.clickable td:nth-child(2)").allInnerTexts();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByText("Page 2").waitFor();
  const secondRows = await page.locator("tbody tr.clickable td:nth-child(2)").allInnerTexts();
  await check("live-activity page 2 has different rows", secondRows.length > 0 && !secondRows.some((r) => firstRows.includes(r)),
    { page1: firstRows.slice(0, 3), page2: secondRows.slice(0, 3) });
  await page.getByRole("button", { name: "Newest" }).click();
  await page.getByText("Page 1").waitFor();

  await page.getByRole("tab", { name: /Investigation/ }).click();
  await page.getByRole("tab", { name: /^Open/ }).click();
  await sleep(2_000);
  const q1 = await page.locator("tbody tr.clickable td:nth-child(2)").allInnerTexts();
  const openCount = (await api("/api/queue?status=open&limit=20")).body.counts.open;
  if (openCount > 20) {
    await page.getByRole("button", { name: "Next" }).click();
    await page.getByText("Page 2").waitFor();
    const q2 = await page.locator("tbody tr.clickable td:nth-child(2)").allInnerTexts();
    await check("investigation page 2 has different rows", q2.length > 0 && !q2.some((r) => q1.includes(r)), { open: openCount, page1: q1.slice(0, 3), page2: q2.slice(0, 3) });
    await page.getByRole("button", { name: "Newest" }).click();
    await page.getByText("Page 1").waitFor();
  } else {
    // A fresh deployment may have a single page of open reviews: Next must then be disabled.
    const nextDisabled = await page.getByRole("button", { name: "Next" }).isDisabled();
    await check("investigation shows every open review on one page and disables Next", nextDisabled && q1.length === openCount, { open: openCount, rows: q1.length, next_disabled: nextDisabled });
  }
  await page.getByRole("tab", { name: /Live activity/ }).click();
  await page.getByText("Demo traffic").waitFor();
  await page.getByLabel("Job").selectOption("failure_drill");
  await page.getByLabel("Rate").selectOption("5");
  await page.getByRole("button", { name: "Start" }).click();
  const t0 = Date.now();
  note("failure drill started from the UI");
  await page.getByLabel("Time range").selectOption("15m");
  const at = async (s) => sleep(Math.max(0, t0 + s * 1000 - Date.now()));
  await at(45);
  const degraded = await readyz();
  const degradedHealth = await api("/api/health");
  await check("drill phase 1: degraded, worker stopped, still scoring",
    degraded.status === "degraded" && degraded.pipeline?.suppress_scoring == null && degradedHealth.body.processes.worker.state === "stopped by failure drill",
    { status: degraded.status, degraded: degraded.degraded, suppress_scoring: degraded.pipeline?.suppress_scoring, worker: degradedHealth.body.processes.worker });
  await shot("10-drill-degraded", { fullPage: true });
  await at(100);
  const during = await readyz();
  const suppressed = during.pipeline?.suppress_scoring === "PIPELINE_STALE_BEYOND_LIMIT";
  await check("drill phase 2: scoring suppressed (suppress_scoring is the stale-limit reason)", suppressed === true,
    { status: during.status, suppress_scoring: during.pipeline?.suppress_scoring, heartbeat_age_s: during.pipeline?.worker_heartbeat_age_s });
  await page.getByRole("tab", { name: /Model & system health/ }).click();
  await page.getByRole("heading", { name: "Running deployment" }).waitFor();
  await sleep(3_000);
  await shot("11-drill-scoring-suppressed", { fullPage: true });
  await at(150);
  const recovered = await pollUntil(async () => {
    const r = await readyz();
    const h = await api("/api/health");
    const j = await api("/api/jobs");
    const drill = j.body.jobs.find((x) => x.kind === "failure_drill");
    const p = r.pipeline ?? {};
    return {
      ok: r.status === "ready" && p.suppress_scoring == null && p.outbox_backlog === 0 && p.consumer_lag_events === 0
        && h.body.processes.worker.state === "running" && drill?.status === "succeeded" && (drill?.outcomes.scoreless_review ?? 0) > 0,
      status: r.status, suppress_scoring: p.suppress_scoring ?? null, backlog: p.outbox_backlog, lag: p.consumer_lag_events,
      worker: h.body.processes.worker, drill: drill && { id: drill.id, status: drill.status, sent: drill.sent, outcomes: drill.outcomes, message: drill.message },
    };
  }, 90_000);
  await check("drill phase 3: recovered (ready, not suppressed, drained, worker running, drill succeeded with scoreless reviews)", recovered.ok, recovered);
  await page.getByRole("tab", { name: /Live activity/ }).click();
  await sleep(8_000);
  await shot("12-drill-recovered", { fullPage: true });

  // 8. Logout invalidates the session, including the old cookie.
  const oldCookie = (await context.cookies()).find((c) => c.name === "fp_console");
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("button", { name: "Sign in" }).waitFor();
  const afterLogout = await api("/api/decisions");
  await context.addCookies([oldCookie]);
  const replayed = await api("/api/decisions");
  await check("logout rejects further reads, including with the old session cookie", afterLogout.status === 401 && replayed.status === 401,
    { after_logout: afterLogout.status, old_cookie_replayed: replayed.status });

  results.console_errors = consoleErrors.filter((e) => !e.includes("ERR_FAILED") && !e.includes("Failed to load resource"));
  await check("no unexpected browser console errors", results.console_errors.length === 0, results.console_errors);
  results.passed = true;
} catch (error) {
  results.passed = false;
  results.error = String(error);
  if (!(error instanceof CheckFailed)) {
    mkdirSync(EVIDENCE, { recursive: true });
    await page.screenshot({ path: `${EVIDENCE}page.png`, fullPage: true }).catch(() => {});
    writeFileSync(`${EVIDENCE}page.html`, await page.content().catch(() => ""));
    results.evidence_dir = EVIDENCE_REL;
  }
  process.exitCode = 1;
} finally {
  results.finished_at = new Date().toISOString();
  mkdirSync(`${ROOT}reports/dashboard/`, { recursive: true });
  writeFileSync(OUT, JSON.stringify(results, null, 2) + "\n");
  await browser.close();
  console.log(results.passed ? "WALKTHROUGH PASSED" : `WALKTHROUGH FAILED: ${results.error}`);
}
