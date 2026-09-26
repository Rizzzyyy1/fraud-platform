# Screenshots: historical captures

These images are **historical records of what the dashboard showed when they were captured**. They
are not evidence of any deployment's current health. A running deployment's state is shown only by
its own dashboard and `GET /api/health` at the time of viewing.

| Files | Captured (UTC) | Source | What the state was |
|---|---|---|---|
| `01`–`15` | 2026-09-25, 20:26–20:32 | Scripted headless-Chrome walkthrough of the local demo deployment (`dashboard/e2e/walkthrough.mjs`). Checks and observed values are in `reports/dashboard/walkthrough.json`. | The state during that run. `07-health.png` shows a healthy pipeline at that time. `10`–`12` are the deliberate failure drill (worker stopped, then recovered). `09` (console unreachable), `14` (loading) and `15` (error) are **injected** states: the walkthrough blocked, delayed or failed requests in the browser to show those views. `13` is an empty filter result. |
| `16-real-data-benchmark.png` | 2026-09-26 | Temporary console built from this repository's code against a disposable, seeded test database (the benchmark entry is read from the committed report files). | Shows only the historical-results entry for the offline ULB benchmark: committed report figures, labelled "offline only · not deployed". It says nothing about live serving. It carries the ODbL notice for the data (see `THIRD_PARTY_NOTICES.md`). |

To see current state, run the deployment (`make dashboard`) and open the health view, or query
`/api/health`.
