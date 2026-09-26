"""Console building blocks without services: accounts, sessions, job specs, telemetry, cursors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fraudplat.console.auth import MAX_FAILURES, Accounts, Analyst, Sessions, verify_password
from fraudplat.console.deployment import demo_requests, new_deployment
from fraudplat.console.jobs import (
    DRILL_DURATION_S,
    HOLD_FILE,
    JobRejected,
    JobSpec,
    classify,
    read_hold,
    release_hold,
)
from fraudplat.console.queries import (
    decode_decisions_cursor,
    decode_queue_cursor,
    encode_cursor,
)
from fraudplat.console.reports import REPORTS, report_text
from fraudplat.console.telemetry import Snapshot, parse_prometheus, window_metrics

from ..features.helpers import event, historical, us


def test_accounts_store_only_hashes(tmp_path: Path) -> None:
    accounts = Accounts(tmp_path / "analysts.json")
    accounts.add("alice", "correct horse battery", "admin")
    raw = (tmp_path / "analysts.json").read_text()
    assert "correct horse battery" not in raw
    assert json.loads(raw)["alice"]["password"].startswith("scrypt$")
    assert (tmp_path / "analysts.json").stat().st_mode & 0o777 == 0o600
    assert accounts.authenticate("alice", "correct horse battery") == Analyst("alice", "admin")
    assert accounts.authenticate("alice", "wrong password!!") is None
    assert accounts.authenticate("nobody", "correct horse battery") is None


def test_accounts_reject_weak_input(tmp_path: Path) -> None:
    accounts = Accounts(tmp_path / "a.json")
    with pytest.raises(ValueError, match="12 characters"):
        accounts.add("bob", "short", "analyst")
    with pytest.raises(ValueError, match="name"):
        accounts.add("bob smith", "long enough password", "analyst")


def test_verify_password_rejects_malformed_hashes() -> None:
    assert not verify_password("x", "plain")
    assert not verify_password("x", "md5$00$00")


def test_sessions_expire_revoke_and_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = Sessions()
    token, session = sessions.create(Analyst("alice", "analyst"))
    assert sessions.get(token) is session
    assert sessions.get("forged") is None and sessions.get(None) is None
    sessions.revoke(token)
    assert sessions.get(token) is None
    for _ in range(MAX_FAILURES):
        assert not sessions.throttled("mallory", now=100.0)
        sessions.record_failure("mallory", now=100.0)
    assert sessions.throttled("mallory", now=101.0)
    assert not sessions.throttled("mallory", now=100.0 + 301)  # window passed


def test_job_spec_accepts_only_enumerated_parameters() -> None:
    assert JobSpec.validate("traffic", 5, 60).requests == 300
    drill = JobSpec.validate("failure_drill", 5, None)
    assert drill.duration_s == DRILL_DURATION_S
    for kind, rate, duration in [
        ("traffic", 7, 60),
        ("traffic", 5, 61),
        ("traffic", 5, None),
        ("failure_drill", 20, None),
        ("shell", 5, 60),
    ]:
        with pytest.raises(JobRejected):
            JobSpec.validate(kind, rate, duration)


def test_hold_file_is_owned_by_its_job_and_expires(tmp_path: Path) -> None:
    (tmp_path / HOLD_FILE).write_text(json.dumps({"job_id": 7, "until": 9e12}))
    assert read_hold(tmp_path) == {"job_id": 7, "until": 9e12}
    release_hold(tmp_path, 8)  # another job cannot release it
    assert (tmp_path / HOLD_FILE).exists()
    release_hold(tmp_path, 7)
    assert not (tmp_path / HOLD_FILE).exists()
    (tmp_path / HOLD_FILE).write_text(json.dumps({"job_id": 7, "until": 1.0}))
    assert read_hold(tmp_path) is None  # expired holds are ignored


def test_classify_never_reports_a_missing_score_as_a_score() -> None:
    assert classify(201, {"action": "review", "score": None}) == "scoreless_review"
    assert classify(201, {"action": "approve", "score": 0.1}) == "approve"
    assert classify(200, {"action": "approve", "score": 0.1}) == "idempotent_replay"
    assert classify(503, {"error": "x"}) == "http_503"


def test_demo_requests_are_prefixed_and_ordered_by_decision_time() -> None:
    import polars as pl

    t = {k: us(f"2025-01-10T12:00:{k:02d}") for k in (10, 11, 15, 20, 21, 30, 31)}
    txns = [
        historical(event("A", t[10], customer="C1"), t[11]),
        historical(event("B", t[30], customer="C2"), t[31]),
        historical(event("C", t[20], customer="C1", terminal="T2"), t[21]),
    ]
    frame = pl.DataFrame(
        {
            "transaction_id": ["A", "B", "C"],
            "currency": ["USD"] * 3,
            "event_time": pl.Series([t[10], t[30], t[20]], dtype=pl.Int64).cast(
                pl.Datetime("us", "UTC")
            ),
        }
    )
    requests = demo_requests(txns, frame, cutoff_us=t[15], id_prefix="D1-")
    assert [r["transaction_id"] for r in requests] == ["D1-C", "D1-B"]  # A precedes the cutoff
    assert requests[0]["customer_id"] == "C1"  # customer ids unchanged, so history applies
    assert requests[0]["event_time"] == "2025-01-10T12:00:20.000000Z"


def test_new_deployment_names_a_replay_namespace() -> None:
    d = new_deployment(10, now=1_700_000_000)
    assert d.namespace == "replay:dashboard-1700000000"
    assert d.stream.consumer_group == "fraud-feature-worker.replay.dashboard-1700000000"
    assert d.id_prefix == "D1700000000-"


PROM = """# HELP x
fraud_decision_responses_total{status="201"} %s
fraud_decision_responses_total{status="503"} %s
fraud_stage_seconds_bucket{le="0.01",stage="request_total"} %s
fraud_stage_seconds_bucket{le="0.025",stage="request_total"} %s
fraud_stage_seconds_bucket{le="+Inf",stage="request_total"} %s
fraud_stage_seconds_bucket{le="0.01",stage="inference"} 99
fraud_feature_read_timeouts_total %s
"""


def test_window_metrics_use_differences_between_scrapes() -> None:
    old = parse_prometheus(PROM % (100, 1, 90, 100, 101, 2))
    new = parse_prometheus(PROM % (190, 11, 150, 190, 201, 3))
    m = window_metrics(old, new, 10.0)
    assert m["requests"] == 100 and m["requests_per_second"] == 10.0
    assert m["error_responses"] == 10 and m["error_rate"] == 0.1
    assert m["request_p95_le_ms"] is None  # 95% bound falls in +Inf (90/100 within 25 ms)
    assert m["feature_read_timeouts"] == 1
    quiet = window_metrics(new, new, 10.0)
    assert quiet["requests"] == 0 and quiet["error_rate"] is None


def test_snapshot_states() -> None:
    assert Snapshot().view(100.0)["state"] == "unavailable"
    assert Snapshot(value=1, fetched_at=95.0).view(100.0)["state"] == "fresh"
    assert Snapshot(value=1, fetched_at=10.0).view(100.0)["state"] == "stale"
    assert Snapshot(value=1, fetched_at=99.0, error="ConnectError").view(100.0)["state"] == "stale"


def _raw(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode()


def test_cursors_round_trip() -> None:
    t = "2026-01-01T00:00:00+00:00"
    time, txn = decode_decisions_cursor(encode_cursor([t, "T1"])) or (None, None)
    assert txn == "T1" and time is not None and time.utcoffset() is not None
    assert decode_queue_cursor(encode_cursor([0.25, t, "T1"])) is not None
    assert decode_queue_cursor(encode_cursor([-1, t, "T1"])) is not None  # scoreless priority
    assert decode_decisions_cursor(None) is None and decode_queue_cursor("") is None


BAD_DECISION_CURSORS = [
    "!!!not-base64",
    _raw("not json"),
    _raw('{"a": 1}'),
    _raw("[]"),
    _raw('["2026-01-01T00:00:00+00:00"]'),
    _raw('["2026-01-01T00:00:00+00:00", "T1", "extra"]'),
    _raw('[null, "T1"]'),
    _raw('[1767225600, "T1"]'),
    _raw('["2026-01-01T00:00:00", "T1"]'),  # no timezone
    _raw('["yesterday", "T1"]'),
    _raw('["2026-01-01T00:00:00+00:00", 42]'),
    _raw('["2026-01-01T00:00:00+00:00", ""]'),
    _raw('["2026-01-01T00:00:00+00:00", "' + "x" * 65 + '"]'),
    "A" * 600,
]

BAD_QUEUE_CURSORS = [
    _raw('[0.5, "2026-01-01T00:00:00+00:00"]'),
    _raw('["0.5", "2026-01-01T00:00:00+00:00", "T1"]'),
    _raw('[true, "2026-01-01T00:00:00+00:00", "T1"]'),
    _raw('[NaN, "2026-01-01T00:00:00+00:00", "T1"]'),
    _raw('[Infinity, "2026-01-01T00:00:00+00:00", "T1"]'),
    _raw('[null, "2026-01-01T00:00:00+00:00", "T1"]'),
    _raw('[0.5, "2026-01-01T00:00:00", "T1"]'),
    _raw('[0.5, 5, "T1"]'),
    _raw("[]"),
]


@pytest.mark.parametrize("cursor", BAD_DECISION_CURSORS)
def test_malformed_decision_cursors_raise_value_error_only(cursor: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_decisions_cursor(cursor)


@pytest.mark.parametrize("cursor", BAD_QUEUE_CURSORS + BAD_DECISION_CURSORS[:4])
def test_malformed_queue_cursors_raise_value_error_only(cursor: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_queue_cursor(cursor)


def test_reports_are_served_by_key_only() -> None:
    assert report_text("../../.env") is None
    assert report_text("unknown") is None
    assert all(not path.startswith("/") and ".." not in path for _t, path in REPORTS.values())
