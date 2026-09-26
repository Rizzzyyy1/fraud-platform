"""Explicit topic creation (broker auto-creation is disabled). Idempotent.

Run: `python -m fraudplat.pipeline.topics --namespace live`
"""

from __future__ import annotations

import argparse

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import (  # type: ignore[attr-defined]  # not re-exported in stubs
    AdminClient,
    NewTopic,
)

from fraudplat.pipeline.streams import (
    DLQ_RETENTION_MS,
    PARTITIONS,
    REPLICATION_FACTOR,
    TOPIC_RETENTION_MS,
    Stream,
)


def ensure_topics(bootstrap: str, stream: Stream, timeout_s: float = 15.0) -> list[str]:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    wanted = [
        NewTopic(
            stream.topic,
            num_partitions=PARTITIONS,
            replication_factor=REPLICATION_FACTOR,
            config={"retention.ms": str(TOPIC_RETENTION_MS), "cleanup.policy": "delete"},
        ),
        NewTopic(
            stream.dlq_topic,
            num_partitions=1,
            replication_factor=REPLICATION_FACTOR,
            config={"retention.ms": str(DLQ_RETENTION_MS), "cleanup.policy": "delete"},
        ),
    ]
    created = []
    for name, future in admin.create_topics(wanted, request_timeout=timeout_s).items():
        try:
            future.result(timeout=timeout_s)
            created.append(name)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
    return created


def delete_topics(bootstrap: str, stream: Stream, timeout_s: float = 15.0) -> None:
    """Replay streams only; the live topics are never deleted by code."""
    if stream.is_live:
        raise ValueError("refusing to delete live topics")
    admin = AdminClient({"bootstrap.servers": bootstrap})
    for future in admin.delete_topics([stream.topic, stream.dlq_topic]).values():
        try:
            future.result(timeout=timeout_s)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.UNKNOWN_TOPIC_OR_PART:
                raise


def delete_consumer_group(bootstrap: str, stream: Stream, timeout_s: float = 15.0) -> None:
    """Replay streams only: remove the run's consumer group (and its committed offsets)."""
    if stream.is_live:
        raise ValueError("refusing to delete the live consumer group")
    admin = AdminClient({"bootstrap.servers": bootstrap})
    # A group that never committed (or a recreated broker) has no coordinator to ask, so the
    # delete call would fail with NOT_COORDINATOR; an absent group is already deleted.
    listed = admin.list_consumer_groups(request_timeout=timeout_s).result(timeout=timeout_s)
    if stream.consumer_group not in {g.group_id for g in listed.valid}:
        return
    for future in admin.delete_consumer_groups([stream.consumer_group]).values():
        try:
            future.result(timeout=timeout_s)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.GROUP_ID_NOT_FOUND:
                raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", default="127.0.0.1:9094")
    parser.add_argument("--namespace", default="live")
    args = parser.parse_args()
    stream = Stream(args.namespace)
    created = ensure_topics(args.bootstrap, stream)
    print(f"topics for {stream.namespace}: {stream.topic}, {stream.dlq_topic} (created: {created})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
