"""Telemetry for the console, gathered on demand and cached so polling stays cheap.

* `/readyz` and `/metrics` of the scoring API: fetched at most once per `min_interval_s`
  however many browser tabs poll. Scrapes are kept for a short window so request rate, error
  rate and a latency upper bound can be computed from counter/histogram *differences*.
* The registry alias target: looked up at most every 30 s, from metadata only.

Every value carries its fetch time; a failed fetch keeps the last good value and reports it as
unavailable or stale instead of hiding it.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from fraudplat.registry import alias_target

STALE_AFTER_S = 30.0
WINDOW_S = 60.0


def parse_prometheus(text: str) -> dict[str, float]:
    """Samples as `name{labels}` → value (enough for our own counters and histograms)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        try:
            out[key] = float(value)
        except ValueError:
            continue
    return out


def _sum_prefix(sample: dict[str, float], prefix: str) -> dict[str, float]:
    return {k: v for k, v in sample.items() if k.startswith(prefix)}


def window_metrics(old: dict[str, float], new: dict[str, float], seconds: float) -> dict[str, Any]:
    """Rates and latency bound between two scrapes of the same process."""
    responses_new = _sum_prefix(new, "fraud_decision_responses_total{")
    by_status: dict[str, float] = {}
    for key, value in responses_new.items():
        status = key.split('status="', 1)[1].split('"', 1)[0]
        by_status[status] = value - old.get(key, 0.0)
    total = sum(by_status.values())
    errors = sum(v for s, v in by_status.items() if not s.startswith("2"))
    buckets: list[tuple[float, float]] = []
    for key, value in new.items():
        if key.startswith("fraud_stage_seconds_bucket{") and 'stage="request_total"' in key:
            le = key.split('le="', 1)[1].split('"', 1)[0]
            bound = float("inf") if le == "+Inf" else float(le)
            buckets.append((bound, value - old.get(key, 0.0)))
    buckets.sort()
    p95_le_ms: float | None = None
    count = buckets[-1][1] if buckets else 0.0
    if count > 0:
        for bound, cumulative in buckets:
            if cumulative >= 0.95 * count:
                p95_le_ms = None if bound == float("inf") else bound * 1000
                break
    timeouts = new.get("fraud_feature_read_timeouts_total", 0.0) - old.get(
        "fraud_feature_read_timeouts_total", 0.0
    )
    return {
        "window_seconds": round(seconds, 1),
        "requests": int(total),
        "requests_per_second": round(total / seconds, 2) if seconds > 0 else None,
        "responses_by_status": {s: int(v) for s, v in sorted(by_status.items()) if v},
        "error_responses": int(errors),
        "error_rate": round(errors / total, 4) if total else None,
        "request_p95_le_ms": p95_le_ms,
        "feature_read_timeouts": int(timeouts),
    }


@dataclass
class Snapshot:
    value: Any = None
    fetched_at: float | None = None  # wall time of the last successful fetch
    error: str | None = None
    attempted_at: float | None = None

    def view(self, now: float) -> dict[str, Any]:
        age = None if self.fetched_at is None else round(now - self.fetched_at, 1)
        if self.fetched_at is None:
            state = "unavailable"
        elif self.error is not None or (age is not None and age > STALE_AFTER_S):
            state = "stale"
        else:
            state = "fresh"
        return {
            "state": state,
            "fetched_at": self.fetched_at,
            "age_seconds": age,
            "error": self.error,
            "value": self.value,
        }


@dataclass
class Telemetry:
    api_url: str
    tracking_uri: str
    model_uri: str | None
    min_interval_s: float = 5.0
    registry_interval_s: float = 30.0
    readyz: Snapshot = field(default_factory=Snapshot)
    registry: Snapshot = field(default_factory=Snapshot)
    scrapes: deque[tuple[float, dict[str, float]]] = field(default_factory=lambda: deque(maxlen=40))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _client: httpx.AsyncClient | None = None
    fetches: int = 0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.api_url, timeout=2.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def refresh(self) -> None:
        async with self._lock:
            now = time.time()
            if self.readyz.attempted_at and now - self.readyz.attempted_at < self.min_interval_s:
                return
            self.readyz.attempted_at = now
            self.fetches += 1
            http = await self._http()
            try:
                ready, metrics = await asyncio.gather(http.get("/readyz"), http.get("/metrics"))
                self.readyz.value = {"http_status": ready.status_code, **ready.json()}
                self.readyz.fetched_at, self.readyz.error = now, None
                if metrics.status_code == 200:
                    self.scrapes.append((now, parse_prometheus(metrics.text)))
            except (httpx.HTTPError, ValueError) as exc:
                self.readyz.error = type(exc).__name__
            await self._refresh_registry(now)

    async def _refresh_registry(self, now: float) -> None:
        if self.model_uri is None:
            return
        attempted = self.registry.attempted_at
        if attempted and now - attempted < self.registry_interval_s:
            return
        self.registry.attempted_at = now
        try:
            target = await asyncio.to_thread(alias_target, self.tracking_uri, self.model_uri)
            self.registry.value = {
                "uri": self.model_uri,
                "registry_ref": target.registry_ref,
                "model_version": target.model_version,
                "policy_version": target.policy_version,
            }
            self.registry.fetched_at, self.registry.error = now, None
        except Exception as exc:
            self.registry.error = type(exc).__name__

    def live_window(self) -> dict[str, Any] | None:
        """Metrics between the newest scrape and the oldest one inside the window. A restart of
        the API resets its counters; a pair spanning a restart is not used."""
        if len(self.scrapes) < 2:
            return None
        newest_t, newest = self.scrapes[-1]
        older = [(t, s) for t, s in self.scrapes if newest_t - t <= WINDOW_S and t < newest_t]
        if not older:
            return None
        t0, old = older[0]
        metrics = window_metrics(old, newest, newest_t - t0)
        if metrics["requests"] < 0 or any(v < 0 for v in metrics["responses_by_status"].values()):
            return None  # counters went backwards: the API restarted inside the window
        return {"measured_at": newest_t, **metrics}
