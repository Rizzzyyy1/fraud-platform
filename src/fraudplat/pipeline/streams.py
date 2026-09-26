"""Stream naming: one namespace determines every name, so live and replay cannot be mixed.

  live          → outbox stream `live`, topic `fraud.decisions.v1`,
                  DLQ `fraud.decisions.dlq.v1`, group `fraud-feature-worker.live`, Redis `live:`
  replay:<run>  → outbox stream `replay:<run>`, topic `fraud.replay.<run>.decisions.v1`,
                  DLQ `fraud.replay.<run>.decisions.dlq.v1`,
                  group `fraud-feature-worker.replay.<run>`, Redis `replay:<run>:`

Topics: 6 partitions keyed by customer_id (all of a customer's events are ordered within one
partition); replication factor 1 on the single local broker. Terminal-level state does not rely
on partition ordering: updates commute within the retention horizon (docs/DESIGN.md §5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PARTITIONS = 6
REPLICATION_FACTOR = 1
TOPIC_RETENTION_MS = 7 * 24 * 3600 * 1000
DLQ_RETENTION_MS = 30 * 24 * 3600 * 1000
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Stream:
    namespace: str

    def __post_init__(self) -> None:
        if self.namespace == "live":
            return
        prefix, _, run = self.namespace.partition(":")
        if prefix != "replay" or not _RUN_ID.match(run):
            raise ValueError("namespace must be 'live' or 'replay:<run-id>' ([a-z0-9-])")

    @property
    def is_live(self) -> bool:
        return self.namespace == "live"

    @property
    def _base(self) -> str:
        return "fraud" if self.is_live else f"fraud.replay.{self.namespace.split(':', 1)[1]}"

    @property
    def topic(self) -> str:
        return f"{self._base}.decisions.v1"

    @property
    def dlq_topic(self) -> str:
        return f"{self._base}.decisions.dlq.v1"

    @property
    def consumer_group(self) -> str:
        suffix = "live" if self.is_live else "replay." + self.namespace.split(":", 1)[1]
        return f"fraud-feature-worker.{suffix}"

    @property
    def worker_status_key(self) -> str:
        return f"{self.namespace}:pipeline:worker"
