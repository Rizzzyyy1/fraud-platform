// Browser smoke test for CI, run by scripts/browser_smoke.py against the real console and the
// production dashboard build, with a seeded disposable database. It does not exercise the scoring
// API, Kafka, Redis or the worker (the health view must report the scoring API as unavailable).
//
// Every check is an assertion; on failure a screenshot, the page HTML and the failed check are
// written to $EVIDENCE_DIR. Credentials come from the environment and are never logged or saved.

import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.CONSOLE_URL;
const USER = process.env.E2E_USER;
const PASSWORD = process.env.E2E_PASSWORD;
const EVIDENCE = (process.env.EVIDENCE_DIR ?? "smoke-evidence").replace(/\/?$/, "/");
if (!BASE || !USER || !PASSWORD) throw new Error("CONSOLE_URL, E2E_USER and E2E_PASSWORD are required");
mkdirSync(EVIDENCE, { recursive: true });

const MODEL = "lr-f1-6f0ebad8fcc7";
const POLICY = "pol-6164cb21826d";
const REGISTRY = "fraud-risk-f1/1";
const results = { started_at: new Date().toISOString(), checks: [] };
class CheckFailed extends Error {}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
page.setDefaultTimeout(15_000);
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

async function check(name, ok, observed) {
  results.checks.push({ name, passed: Boolean(ok), observed });
  console.log(`${ok ? "✓" : "✗"} ${name}`, JSON.stringify(observed));
  if (ok) return;
  await page.screenshot({ path: `${EVIDENCE}failure.png`, fullPage: true }).catch(() => {});
  writeFileSync(`${EVIDENCE}failure.html`, await page.content().catch(() => ""));
  writeFileSync(`${EVIDENCE}failed-check.json`, JSON.stringify({ name, observed, url: page.url() }, null, 2));
  throw new CheckFailed(`check failed: ${name}`);
}
const api = (path, init) =>
  page.evaluate(async ([p, i]) => {
    const r = await fetch(p, { credentials: "same-origin", ...i });
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    return { status: r.status, body };
  }, [path, init]);
const rowIds = () => page.locator("tbody tr.clickable td:nth-child(2)").allInnerTexts();
async function nextPageDiffers(name) {
  await page.locator("tbody tr.clickable").first().waitFor();
  const first = await rowIds();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByText("Page 2").waitFor();
  const second = await rowIds();
  await check(name, first.length > 0 && second.length > 0 && !second.some((r) => first.includes(r)),
    { page1: first.length, page2: second.length, overlap: second.filter((r) => first.includes(r)).length });
  await page.getByRole("button", { name: "Newest" }).click();
  await page.getByText("Page 1").waitFor();
}

try {
  // Anonymous access and sign-in.
  await page.goto(BASE);
  await page.getByRole("button", { name: "Sign in" }).waitFor();
  const anon = await Promise.all(["/api/decisions", "/api/queue", "/api/health", "/api/jobs"].map((p) => api(p)));
  await check("anonymous API reads are rejected with 401", anon.every((r) => r.status === 401), anon.map((r) => r.status));
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByText("Simulated data", { exact: true }).waitFor();
  const cookie = (await context.cookies()).find((c) => c.name === "fp_console");
  await check("session cookie is HttpOnly and SameSite=Strict", cookie?.httpOnly === true && cookie?.sameSite === "Strict",
    { httpOnly: cookie?.httpOnly, sameSite: cookie?.sameSite });
  await check("session cookie is not readable from page scripts",
    (await page.evaluate(() => document.cookie.includes("fp_console"))) === false, {});

  // Main views load.
  await page.locator("tbody tr.clickable").first().waitFor();
  await check("live activity lists decisions", (await rowIds()).length === 25, { rows: (await rowIds()).length });
  await nextPageDiffers("live-activity pagination: page 2 has different decisions");
  await page.getByRole("tab", { name: /Model & system health/ }).click();
  await page.getByRole("heading", { name: "Running deployment" }).waitFor();
  const unavailable = await page.getByText(/Running API: unavailable/).count();
  await check("health view loads and reports the scoring API unavailable (not run in the smoke test)", unavailable === 1, { unavailable_badges: unavailable });
  const benchmark = await page.getByRole("heading", { name: "Real-data offline benchmark (ULB)" }).count();
  const notDeployed = await page.getByText("Real data · offline only · not deployed").count();
  await check("real-data benchmark shown as offline and not deployed", benchmark === 1 && notDeployed === 1, { benchmark, notDeployed });
  await page.getByRole("tab", { name: /Investigation/ }).click();
  await page.getByRole("tab", { name: /^Open/ }).waitFor();
  const open = (await api("/api/queue?status=open&limit=20")).body.counts.open;
  await check("investigation queue has more than one page of open reviews", open > 20, { open });
  await nextPageDiffers("investigation pagination: page 2 has different reviews");

  // Inspect an approved decision and its identities.
  await page.getByRole("tab", { name: /Live activity/ }).click();
  await page.getByLabel("Action").selectOption("approve");
  await page.locator("tbody tr.clickable").first().waitFor();
  await page.locator("tbody tr.clickable").first().click();
  await page.getByText("From transaction to auditable decision").waitFor();
  const detail = await page.locator('[aria-label="Decision detail"]').innerText();
  await check("decision detail shows the stored model, policy and registry identities",
    detail.includes(MODEL) && detail.includes(POLICY) && detail.includes(REGISTRY) && detail.includes("review-only"),
    { model: detail.includes(MODEL), policy: detail.includes(POLICY), registry: detail.includes(REGISTRY) });
  await check("decision summary states the policy rule, not a feature explanation",
    /< review threshold 0\.0381 → approve/.test(detail) && detail.includes("does not say which features drove the score"), {});
  await check("stored feature values are shown", detail.includes("amount_to_cust_mean_30d") && detail.includes("Reads as"), {});

  // Save a review on a reviewed decision without changing the decision.
  await page.getByRole("tab", { name: /Investigation/ }).click();
  await page.getByRole("tab", { name: /^Open/ }).click();
  await page.locator("tbody tr.clickable").first().waitFor();
  const row = page.locator("tbody tr.clickable").first();
  const txn = (await row.locator("td").nth(1).innerText()).trim();
  await row.click();
  await page.getByText("From transaction to auditable decision").waitFor();
  const before = await api(`/api/decisions/${encodeURIComponent(txn)}`);
  await page.getByLabel("New review status").selectOption("closed");
  await page.getByLabel("Disposition").selectOption("needs_more_information");
  await page.getByPlaceholder(/Analyst note/).fill("Smoke test review.");
  await page.getByRole("button", { name: "Save review" }).click();
  await page.getByText("Review saved.").waitFor();
  const after = await api(`/api/decisions/${encodeURIComponent(txn)}`);
  await check("review persisted", after.body.reviews.at(-1)?.status === "closed" && after.body.reviews.at(-1)?.disposition === "needs_more_information",
    { reviews: after.body.reviews.length });
  await check("original decision unchanged by the review", JSON.stringify(before.body.decision) === JSON.stringify(after.body.decision), { transaction_id: txn });

  // Logout invalidates the session, including the old cookie.
  const oldCookie = (await context.cookies()).find((c) => c.name === "fp_console");
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("button", { name: "Sign in" }).waitFor();
  const afterLogout = await api("/api/decisions");
  await context.addCookies([oldCookie]);
  const replayed = await api("/api/decisions");
  await check("logout rejects reads, including with the old session cookie", afterLogout.status === 401 && replayed.status === 401,
    { after_logout: afterLogout.status, old_cookie_replayed: replayed.status });
  // Expected: "Failed to load resource" for the anonymous 401 checks above.
  const unexpected = consoleErrors.filter((e) => !e.includes("Failed to load resource"));
  await check("no unexpected browser console errors", unexpected.length === 0, { unexpected });
  results.passed = true;
} catch (error) {
  results.passed = false;
  results.error = String(error);
  if (!(error instanceof CheckFailed)) {
    await page.screenshot({ path: `${EVIDENCE}failure.png`, fullPage: true }).catch(() => {});
    writeFileSync(`${EVIDENCE}failure.html`, await page.content().catch(() => ""));
  }
  process.exitCode = 1;
} finally {
  results.finished_at = new Date().toISOString();
  writeFileSync(`${EVIDENCE}smoke-results.json`, JSON.stringify(results, null, 2) + "\n");
  await browser.close();
  console.log(results.passed ? "SMOKE PASSED" : `SMOKE FAILED: ${results.error}`);
}
