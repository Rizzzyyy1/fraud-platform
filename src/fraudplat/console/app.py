"""Console service (backend-for-frontend) for the analyst dashboard.

Runs locally beside the scoring API. The browser authenticates as an analyst (session cookie);
the scoring API key stays in this process's environment and is never sent to the browser.

Endpoints (all under /api; everything except login requires a session; POST/DELETE also require
the session's CSRF token in `X-CSRF-Token`; jobs require the admin role):
  POST/GET/DELETE /api/session           login, current analyst, logout
  GET  /api/decisions                     live activity: range, filters, keyset pagination
  GET  /api/activity                      per-bucket outcome counts for the range
  GET  /api/decisions/{id}                stored decision + review history (read-only)
  GET  /api/queue                         review queue by status, prioritised by score
  POST /api/decisions/{id}/reviews        append an analyst review (never edits the decision)
  GET  /api/health                        running model vs registry target, pipeline, telemetry
  GET  /api/results                       historical results by split, from committed reports
  GET  /api/reports/{key}                 one whitelisted report's text
  GET/POST /api/jobs, POST /api/jobs/{id}/cancel   bounded demo jobs
The built dashboard (`dashboard/dist`) is served at `/`.

Nothing here runs on the scoring path: reads use a separate 4-connection pool with a 2 s
statement timeout, and API telemetry is fetched at most every 5 s regardless of tab count.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Histogram
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from fraudplat.config import Settings
from fraudplat.console.auth import Accounts, Analyst, Session, Sessions
from fraudplat.console.deployment import STATE_DIR, Deployment, load_deployment, read_pids
from fraudplat.console.deployment import process_state as ps_state
from fraudplat.console.jobs import JobConflict, JobManager, JobRejected, JobSpec, read_hold
from fraudplat.console.queries import (
    ConsoleStore,
    Disposition,
    InvalidReview,
    NotFound,
    ReviewStatus,
)
from fraudplat.console.reports import (
    REPORTS,
    historical_results,
    policy_rules,
    release_status,
    report_text,
)
from fraudplat.console.telemetry import Telemetry

COOKIE = "fp_console"
CONSOLE_REQUEST = Histogram(
    "fraud_console_request_seconds",
    "Console endpoint duration",
    ["endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
DIST = Path("dashboard/dist")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ReviewStatus
    disposition: Disposition | None = None
    note: str | None = Field(default=None, max_length=2000)


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["traffic", "failure_drill"]
    rate: int
    duration_s: int | None = None


def current(request: Request) -> Session:
    sessions: Sessions = request.app.state.sessions
    session = sessions.get(request.cookies.get(COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return session


def mutation(request: Request, x_csrf_token: Annotated[str | None, Header()] = None) -> Session:
    session = current(request)
    if x_csrf_token is None or not secrets.compare_digest(x_csrf_token, session.csrf):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
    return session


def admin(session: Annotated[Session, Depends(mutation)]) -> Session:
    if session.analyst.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return session


Current = Annotated[Session, Depends(current)]
Mutation = Annotated[Session, Depends(mutation)]
Admin = Annotated[Session, Depends(admin)]


def _session_view(session: Session) -> dict[str, Any]:
    return {
        "analyst": session.analyst.name,
        "role": session.analyst.role,
        "csrf_token": session.csrf,
    }


def create_console(
    settings: Settings | None = None,
    deployment: Deployment | None = None,
    state_dir: Path = STATE_DIR,
    accounts_file: Path | None = None,
    api_url: str | None = None,
    telemetry: Telemetry | None = None,
    jobs: JobManager | None = None,
    secure_cookie: bool = False,
) -> FastAPI:
    settings = settings or Settings()
    deployment = deployment or load_deployment(state_dir)
    if deployment is None:
        raise RuntimeError("no dashboard deployment; run `make dashboard` first")
    accounts = Accounts(accounts_file or state_dir / "analysts.json")
    sessions = Sessions()
    api_port = os.environ.get("FRAUD_CONSOLE_API_PORT", "8110")
    telemetry = telemetry or Telemetry(
        api_url or f"http://127.0.0.1:{api_port}",
        settings.mlflow_tracking_uri,
        os.environ.get("FRAUD_CONSOLE_MODEL_URI", "models:/fraud-risk-f1@production"),
    )
    jobs = jobs or JobManager(settings.database_url.get_secret_value(), deployment, state_dir)
    started_at = time.time()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=1,
            max_size=4,
            timeout=2,
            kwargs={"connect_timeout": 2, "options": "-c timezone=UTC -c statement_timeout=2000"},
            open=False,
        )
        await pool.open(wait=False)
        app.state.store = ConsoleStore(pool, deployment.namespace)
        try:
            yield
        finally:
            await telemetry.close()
            await pool.close()

    app = FastAPI(title="fraud-platform console", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.sessions = sessions

    @app.middleware("http")
    async def timing(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        name = getattr(route, "path", "other") if request.url.path.startswith("/api") else "static"
        CONSOLE_REQUEST.labels(name).observe(time.perf_counter() - start)
        response.headers["Cache-Control"] = "no-store" if name != "static" else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def store(request: Request) -> ConsoleStore:
        s: ConsoleStore = request.app.state.store
        return s

    # ------------------------------------------------------------------ session

    @app.post("/api/session")
    async def login(body: Login, response: Response) -> dict[str, Any]:
        if sessions.throttled(body.username):
            raise HTTPException(status_code=429, detail="too many failed sign-ins; wait 5 minutes")
        analyst: Analyst | None = await asyncio.to_thread(
            accounts.authenticate, body.username, body.password
        )
        if analyst is None:
            sessions.record_failure(body.username)
            raise HTTPException(status_code=401, detail="invalid username or password")
        token, session = sessions.create(analyst)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=secure_cookie,
            path="/",
            max_age=8 * 3600,
        )
        return _session_view(session)

    @app.get("/api/session")
    async def whoami(session: Current) -> dict[str, Any]:
        return _session_view(session)

    @app.delete("/api/session")
    async def logout(request: Request, response: Response, _: Mutation) -> dict[str, str]:
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"status": "signed_out"}

    # ------------------------------------------------------------------ reads

    @app.get("/api/decisions")
    async def decisions(
        request: Request,
        _: Current,
        range: Literal["15m", "1h", "6h", "24h"] = "15m",
        action: Literal["approve", "review", "decline"] | None = None,
        scoring: Literal["scored", "scoreless"] | None = None,
        degraded: bool | None = None,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        try:
            return await store(request).decisions(range, action, scoring, degraded, cursor, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/activity")
    async def activity(
        request: Request,
        _: Current,
        range: Literal["15m", "1h", "6h", "24h"] = "15m",
    ) -> dict[str, Any]:
        return await store(request).activity(range)

    @app.get("/api/decisions/{transaction_id}")
    async def decision(request: Request, transaction_id: str, _: Current) -> dict[str, Any]:
        try:
            found = await store(request).decision(transaction_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="decision not found") from exc
        rules = await asyncio.to_thread(policy_rules)
        return {**found, "policy": rules.get(found["decision"]["policy_version"])}

    @app.get("/api/queue")
    async def queue(
        request: Request,
        _: Current,
        status: Literal["open", "in_review", "closed"] = "open",
        scoring: Literal["scored", "scoreless"] | None = None,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        try:
            return await store(request).queue(status, scoring, cursor, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/decisions/{transaction_id}/reviews", status_code=201)
    async def add_review(
        request: Request, transaction_id: str, body: ReviewIn, session: Mutation
    ) -> dict[str, Any]:
        try:
            return await store(request).add_review(
                transaction_id, body.status, body.disposition, body.note, session.analyst.name
            )
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="decision not found") from exc
        except InvalidReview as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/health")
    async def health(request: Request, _: Current) -> dict[str, Any]:
        await telemetry.refresh()
        now = time.time()
        pids = read_pids(state_dir)
        hold = read_hold(state_dir)
        processes: dict[str, dict[str, Any]] = {}
        for name in ("api", "publisher", "worker", "console"):
            pid = pids.get(name)
            state = await asyncio.to_thread(ps_state, pid) if pid else None
            if state and not state.startswith("Z"):
                label = "running"
            elif name == "worker" and hold:
                label = "stopped by failure drill"
            else:
                label = "not running"
            processes[name] = {"pid": pid, "state": label}
        return {
            "now": now,
            "deployment": {
                "namespace": deployment.namespace,
                "created_at": deployment.created_at,
                "environment": "local (single machine)",
                "data": "simulated (sim-v2, pre-test days 140-152 as demo traffic)",
            },
            "running": telemetry.readyz.view(now),
            "registry_target": telemetry.registry.view(now),
            "live_window": telemetry.live_window(),
            "processes": processes,
            "worker_hold": hold,
            "console": {
                "database": await store(request).ping(),
                "telemetry_fetches": telemetry.fetches,
                "uptime_seconds": round(now - started_at),
            },
        }

    @app.get("/api/results")
    async def results(_: Current) -> dict[str, Any]:
        return {
            "results": historical_results(),
            "release": release_status(),
            "reports": [{"key": k, "title": t} for k, (t, _p) in REPORTS.items()],
        }

    @app.get("/api/reports/{key}")
    async def report(key: str, _: Current) -> dict[str, str]:
        found = report_text(key)
        if found is None:
            raise HTTPException(status_code=404, detail="unknown report")
        return {"key": key, "title": found[0], "markdown": found[1]}

    # ------------------------------------------------------------------ jobs

    @app.get("/api/jobs")
    async def list_jobs(_: Current) -> dict[str, Any]:
        return await asyncio.to_thread(jobs.recent)

    @app.post("/api/jobs", status_code=201)
    async def start_job(body: JobIn, session: Admin) -> dict[str, Any]:
        try:
            spec = JobSpec.validate(body.kind, body.rate, body.duration_s)
            return await asyncio.to_thread(jobs.start, spec, session.analyst.name)
        except JobRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int, _: Admin) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(jobs.cancel, job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/api/{rest:path}")
    async def unknown(rest: str) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "not found"})

    # ------------------------------------------------------------------ static bundle

    if (DIST / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{rest:path}", include_in_schema=False)
        async def spa(rest: str) -> FileResponse:
            return FileResponse(DIST / "index.html")

    return app
