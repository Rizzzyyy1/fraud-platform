// Records the demonstration video from the real running application (headless Chrome via
// playwright-core; video capture needs `npx playwright-core install ffmpeg` once).
//
// Real time, no speed-up, unchanged failure thresholds. Sign-in happens in a separate,
// unrecorded browser context; the recorded context reuses only the session cookie, so no
// credentials or account setup appear in the video. Captions are an overlay added for the
// recording and are labelled as such.
//
// Usage (admin account; credentials from the environment only):
//   E2E_USER=... E2E_PASSWORD=... node e2e/demo_recording.mjs
// Output: media/fraud-platform-demo.webm (git-ignored) and media/demo-timeline.json.

import { chromium } from "playwright-core";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";

const BASE = process.env.CONSOLE_URL ?? "http://127.0.0.1:8200";
const OUT = new URL("../../media/", import.meta.url).pathname;
const SIZE = { width: 1440, height: 900 };
mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const timeline = [];
let t0 = 0;

const browser = await chromium.launch({ channel: "chrome", headless: true });

// 1. Unrecorded sign-in.
const login = await browser.newContext({ viewport: SIZE });
const lp = await login.newPage();
await lp.goto(BASE);
await lp.getByLabel("Username").fill(process.env.E2E_USER ?? "");
await lp.getByLabel("Password").fill(process.env.E2E_PASSWORD ?? "");
await lp.getByRole("button", { name: "Sign in" }).click();
await lp.getByText("Simulated data", { exact: true }).waitFor();
const state = await login.storageState();
// Wait until no demo job is running, before recording starts.
for (let i = 0; i < 120; i++) {
  const jobs = await lp.evaluate(async () => (await fetch("/api/jobs")).json());
  if (!jobs.jobs.some((j) => j.status === "running")) break;
  await sleep(5000);
}
await login.close();

const api = async (page, path, init) =>
  page.evaluate(async ([p, i]) => (await fetch(p, { credentials: "same-origin", ...i })).json(), [path, init]);

// 2. Recorded context.
const context = await browser.newContext({
  viewport: SIZE,
  storageState: state,
  recordVideo: { dir: OUT, size: SIZE },
});
const page = await context.newPage();
const caption = async (text, seconds = 0) => {
  const at = ((Date.now() - t0) / 1000).toFixed(0);
  timeline.push({ at_s: Number(at), caption: text });
  await page.evaluate((t) => {
    let el = document.getElementById("rec-caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "rec-caption";
      el.setAttribute("style", [
        "position:fixed", "left:50%", "bottom:22px", "transform:translateX(-50%)", "z-index:9999",
        "max-width:1100px", "padding:12px 18px", "border-radius:10px", "background:rgba(17,24,39,.92)",
        "color:#fff", "font:500 17px/1.45 system-ui,-apple-system,sans-serif",
        "box-shadow:0 6px 24px rgba(0,0,0,.25)", "pointer-events:none",
      ].join(";"));
      document.body.appendChild(el);
      const tag = document.createElement("div");
      tag.id = "rec-tag";
      tag.textContent = "Real-time recording · captions added for the video";
      tag.setAttribute("style", "position:fixed;right:14px;bottom:6px;z-index:9999;font:11px system-ui;color:#6b7280;pointer-events:none");
      document.body.appendChild(tag);
    }
    el.textContent = t;
  }, text);
  if (seconds) await sleep(seconds * 1000);
};
const go = async (hash) => {
  await page.goto(`${BASE}/#${hash}`);
  await page.waitForTimeout(600);
};

await page.goto(`${BASE}/#/live`);
await page.getByText("Demo traffic").waitFor();
t0 = Date.now();

// Intro.
await caption("A locally deployed fraud decisioning platform, running on synthetic payment data.", 5);
await caption("Every transaction is scored in real time, stored as an auditable decision, and fed back into point-in-time features through Kafka.", 6);

// Start activity.
await page.getByLabel("Job").selectOption("traffic");
await page.getByLabel("Rate").selectOption("10");
await page.getByLabel("Duration").selectOption("60");
await page.getByRole("button", { name: "Start" }).click();
await caption("Start demo traffic: pre-test simulated transactions at 10 per second, through the real API.", 8);
await caption("Each row is a stored decision: amount, model risk score and action. This release is review-only: no automatic declines.", 9);

// Approved decision.
await page.getByLabel("Action").selectOption("approve");
await sleep(1500);
await page.locator("tbody tr.clickable").first().click();
await page.getByText("From transaction to auditable decision").waitFor();
await caption("An approved decision. The summary states the policy rule applied: score below the review threshold.", 7);
await caption("Its lifecycle: received, decided in milliseconds, persisted with its outbox event in one transaction, then published to Kafka.", 8);
await page.getByRole("heading", { name: "Decided by (stored with the decision)" }).scrollIntoViewIfNeeded();
await caption("The exact model, policy and registry version are stored with every decision and cannot be edited.", 7);
await page.getByRole("heading", { name: /Stored feature values/ }).scrollIntoViewIfNeeded();
await caption("The feature values the model actually received, computed only from events available before the decision.", 8);

// Reviewed decision and disposition.
await go("/queue");
await page.getByRole("tab", { name: /^Open/ }).waitFor();
await sleep(1200);
await page.locator("tbody tr.clickable").first().click();
await page.getByText("From transaction to auditable decision").waitFor();
await caption("The review queue, highest score first. This one scored above the review threshold.", 7);
await caption("Reason codes are policy and data-quality flags, not an explanation of which features drove the score.", 6);
await page.getByLabel("New review status").selectOption("closed");
await page.getByLabel("Disposition").selectOption("needs_more_information");
await page.getByPlaceholder(/Analyst note/).fill("Amount about twice this customer's 30-day average; contacting the customer (demo).");
await caption("An analyst records a disposition and a note.", 4);
await page.getByRole("button", { name: "Save review" }).click();
await page.getByText("Review saved.").waitFor();
await page.getByRole("heading", { name: "Analyst review (stored separately)" }).scrollIntoViewIfNeeded();
await caption("Reviews are appended separately: the original decision is unchanged, and a disposition is not a fraud label.", 8);

// Running model.
await go("/health");
await page.getByRole("heading", { name: "Running deployment" }).waitFor();
await caption("Model and system health: the model and policy the running API loaded, next to where the registry alias points now.", 8);
await page.getByRole("heading", { name: "Release candidate" }).scrollIntoViewIfNeeded();
await caption("The XGBoost candidate ranked better on held-out data but is not promoted: it did not reproducibly meet the latency criterion.", 9);

// Failure drill.
await go("/live");
await page.getByLabel("Action").selectOption("");
for (let i = 0; i < 30; i++) {
  const jobs = await api(page, "/api/jobs");
  if (!jobs.jobs.some((j) => j.status === "running")) break;
  await caption("Waiting for the traffic job to finish (one demo job at a time)…", 2);
}
await page.getByLabel("Job").selectOption("failure_drill");
await page.getByLabel("Rate").selectOption("5");
await page.getByRole("button", { name: "Start" }).click();
const drill0 = Date.now();
const at = (s) => sleep(Math.max(0, drill0 + s * 1000 - Date.now()));
await caption("Controlled failure: traffic at 5 per second; at 20 s the feature worker is stopped for 90 s.", 10);
await at(24);
await caption("Worker stopped. Decisions continue; within 10 s of silence they are flagged as degraded.", 0);
await at(36);
await caption("Degraded but still scored: features may be slightly stale, so every decision carries PIPELINE_DEGRADED.", 0);
await at(52);
await caption("Real time, unchanged thresholds: scoring stops only after the worker has been silent for more than 60 s.", 0);
await at(86);
await go("/health");
await page.getByRole("heading", { name: "Running deployment" }).waitFor();
await caption("Past 60 s: scoring is suppressed. New decisions are stored as scoreless reviews instead of trusting stale features.", 0);
await at(102);
await go("/live");
await page.getByLabel("Score").selectOption("scoreless");
await caption("Scoreless reviews are shown as Not scored, never as a zero score.", 0);
await at(114);
await caption("At 110 s the drill releases the worker and the launcher restarts it; it catches up on the events it missed.", 0);
await at(126);
await page.getByLabel("Score").selectOption("");
await caption("Recovered: pipeline healthy again, scoring resumed.", 7);
await go("/health");
await page.getByRole("heading", { name: "Running deployment" }).waitFor();
await caption("Tradeoff: after 60 s of staleness the system chooses a human review over a possibly wrong automatic decision.", 8);
await caption("Limitation: synthetic data only, and both models essentially miss the compromised-terminal fraud scenario.", 8);

const duration = (Date.now() - t0) / 1000;
const video = page.video();
await context.close();
const path = await video.path();
renameSync(path, `${OUT}fraud-platform-demo.webm`);
writeFileSync(`${OUT}demo-timeline.json`, JSON.stringify({ recorded_at: new Date().toISOString(), duration_s: Math.round(duration), speed: "real time", timeline }, null, 2) + "\n");
await browser.close();
console.log(`recorded ${Math.round(duration)} s -> media/fraud-platform-demo.webm`);
