# ADR 0011 — Analyst console as a local backend-for-frontend with append-only reviews

**Status:** accepted

**Context.** The dashboard needs live activity, an investigation queue with analyst notes, and
model/system health. The scoring API is authenticated with a shared API key that must not reach
a browser bundle, and the scoring path must not pay for dashboard queries. Analyst judgment must
not rewrite the audit record or be mistaken for a confirmed label.

**Decision.**
* A separate console service (`fraudplat.console`, FastAPI, 127.0.0.1:8200) serves the built React
  dashboard and a small `/api`. The browser holds only an HttpOnly, SameSite=Strict session
  cookie; state-changing requests also need a per-session CSRF token. Accounts are scrypt hashes
  in a git-ignored local file; job controls require the `admin` role.
* The console reads PostgreSQL through its own 4-connection pool (2 s statement timeout) with
  bounded queries: ≤ 24 h ranges on indexed columns, keyset pagination, ≤ 100 rows per page.
  API telemetry (`/readyz`, `/metrics`) is fetched at most every 5 s regardless of tab count;
  the registry alias target at most every 30 s, from metadata only.
* Reviews go to an append-only `reviews` table (PostgreSQL rules turn UPDATE/DELETE into no-ops;
  a foreign key prevents deleting a reviewed decision). `decisions` is never written by the
  console. Dispositions are analyst judgments, are not labels, and nothing reads them for training.
* Demo traffic and the failure drill are two predefined jobs with enumerated parameters, run as a
  subprocess with a fixed argv, one at a time (partial unique index), each owning a contiguous
  slice of pre-test transactions. The drill stops the deployment's feature worker (pid verified)
  under an expiring hold file; the launcher restarts it when the hold is released.
* The dashboard runs its own `replay:dashboard-<ts>` namespace with prefixed transaction ids, so
  it never touches `live` or benchmark rows.

**Consequences.** Sessions live in memory (a console restart signs analysts out). One console
process only. The console can observe but not change models, policies or the registry.
