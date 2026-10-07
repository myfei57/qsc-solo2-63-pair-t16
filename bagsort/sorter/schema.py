"""The audited change of the dispatch dedup key schema.

Moving the dedup key from bag id to ``(bag id, generation)`` is itself an
event that must be reconstructable after the fact. This module appends a
``dedup.schema`` marker to the journal and, in the same commit, a tamper
evident audit entry. The marker's position in the journal is the boundary
used by reconciliation and by every historical lookup.

The upgrade is idempotent: once v2 is in force, a second call changes
nothing and writes nothing, so replaying an operator action never inserts a
second boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.audit.chain import AuditChain
from bagsort.clock import Clock
from bagsort.model.dedup import KEY_SCHEMA_V1, KEY_SCHEMA_V2, MARKER_KIND
from bagsort.store.store import Store

# The handling stage that owns the key definition. Sorter is where dispatch
# applies the rule; the audit entry records it there for timeline ordering.
SCHEMA_STAGE = "sorter"
SCHEMA_KIND = "dedup_schema"


@dataclass(frozen=True)
class SchemaChange:
    """What happened to the schema when an upgrade was requested."""

    from_schema: int
    to_schema: int
    changed: bool
    at: str
    audit_sequence: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "from": self.from_schema,
            "to": self.to_schema,
            "changed": self.changed,
            "at": self.at,
            "audit_sequence": self.audit_sequence,
        }


class DedupSchemaManager:
    """Appends and inspects the dedup key schema boundary."""

    def __init__(self, store: Store, audit: AuditChain, clock: Clock) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def current(self) -> int:
        markers = self._store.state().dedup_schemas
        if not markers:
            return KEY_SCHEMA_V1
        return int(markers[-1]["to"])

    def is_current(self) -> bool:
        return self.current() == KEY_SCHEMA_V2

    def upgrade(self) -> SchemaChange:
        """Move to the composite key schema; idempotent once on v2."""

        from_schema = self.current()
        if from_schema == KEY_SCHEMA_V2:
            marker = self._store.state().dedup_schemas[-1]
            return SchemaChange(
                from_schema=from_schema,
                to_schema=from_schema,
                changed=False,
                at=str(marker.get("at", "")),
            )
        at = self._clock.now_iso()
        audit_sequence = self._audit.count() + 1
        with self._store.session() as session:
            session.record(
                MARKER_KIND,
                {"from": from_schema, "to": KEY_SCHEMA_V2, "at": at},
                at,
            )
            entry = self._audit.record(
                SCHEMA_KIND,
                "dedup-key",
                SCHEMA_STAGE,
                detail={
                    "from_schema": from_schema,
                    "to_schema": KEY_SCHEMA_V2,
                    "key": "bag_id+generation",
                    "reason": "sequence epoch rollover distinguishes reused bag ids",
                },
                at=at,
                session=session,
            )
            audit_sequence = entry.sequence
        return SchemaChange(
            from_schema=from_schema,
            to_schema=KEY_SCHEMA_V2,
            changed=True,
            at=at,
            audit_sequence=entry.sequence,
        )

    def history(self) -> list[dict[str, object]]:
        return [dict(marker) for marker in self._store.state().dedup_schemas]
