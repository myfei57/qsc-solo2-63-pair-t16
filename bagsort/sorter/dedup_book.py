"""The dispatch deduplication set and the cross checks around it.

Three views must agree about one handling decision:

sequence allocator
    the monotonically wrapping ``(generation, sequence)`` watermark, from
    which a bag's generation is authoritative;
bag ledger
    every ``(bag id, generation)`` ever committed, append only;
sort records
    one record per dispatch, whose ``(bag id, generation)`` is what dispatch
    refuses to repeat.

:class:`DedupLog` reads the materialised ``dedup_keys`` set (one encoded key
per sort record) and exposes the composite membership question dispatch asks.
:meth:`DedupLog.reconcile` verifies that the set, the ledger and the sort
records agree and lists every mismatch individually, which is what an operator
uses to re-check a wrap point instead of trusting a single boolean.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.model.dedup import (
    KEY_SCHEMA_V1,
    KEY_SCHEMA_V2,
    DedupKey,
    decode_key,
    encode_key,
)
from bagsort.store.store import Store


@dataclass(frozen=True)
class Reconciliation:
    """Result of cross checking dedup set, bag ledger and sort records."""

    schema: int
    consistent: bool
    dedup_entries: int
    sort_records: int
    bag_instances: int
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "consistent": self.consistent,
            "dedup_entries": self.dedup_entries,
            "sort_records": self.sort_records,
            "bag_instances": self.bag_instances,
            "issues": list(self.issues),
        }


class DedupLog:
    """Composite ``(bag id, generation)`` membership, rebuilt from the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- membership --------------------------------------------------------

    def keys(self) -> list[DedupKey]:
        return [decode_key(raw) for raw in self._store.state().dedup_keys]

    def contains(self, bag_id: str, generation: int) -> bool:
        return encode_key(bag_id, generation) in self._store.state().dedup_keys

    def generations_for(self, bag_id: str) -> list[int]:
        """Every generation under which an id was dispatched, ascending."""

        return sorted(
            {
                key.generation
                for key in self.keys()
                if key.bag_id == bag_id
            }
        )

    def instances_for(self, bag_id: str) -> list[int]:
        """Every generation under which an id was ever committed, ascending."""

        prefix = f"{bag_id}#"
        generations = [
            decode_key(raw).generation
            for raw in self._store.state().bag_instances
            if raw.startswith(prefix)
        ]
        return sorted(set(generations))

    def schema(self) -> int:
        state = self._store.state()
        if not state.dedup_schemas:
            return KEY_SCHEMA_V1
        return int(state.dedup_schemas[-1]["to"])

    def current_schema(self) -> int:
        return self.schema()

    def schema_boundaries(self) -> list[dict[str, object]]:
        return [dict(marker) for marker in self._store.state().dedup_schemas]

    # -- cross checks ------------------------------------------------------

    def reconcile(self) -> Reconciliation:
        state = self._store.state()
        schema = self.schema()
        issues: list[str] = []

        # 1. one dedup set entry per sort record, same key in the same order.
        if len(state.dedup_keys) != len(state.sort_records):
            issues.append(
                f"dedup set has {len(state.dedup_keys)} entries but "
                f"{len(state.sort_records)} sort records"
            )
        for index, (raw, record) in enumerate(
            zip(state.dedup_keys, state.sort_records)
        ):
            expected = encode_key(record.bag_id, record.epoch)
            if raw != expected:
                issues.append(
                    f"dedup entry {index + 1} is {raw!r} but sort record "
                    f"{index + 1} implies {expected!r}"
                )
        duplicates = sorted(
            {raw for raw in state.dedup_keys if state.dedup_keys.count(raw) > 1}
        )
        for raw in duplicates:
            issues.append(f"dedup key {raw!r} admitted a bag more than once")

        # 2. every dispatched key must correspond to a committed bag instance.
        ledger = set(state.bag_instances)
        for raw in state.dedup_keys:
            if raw not in ledger:
                issues.append(f"dedup key {raw!r} has no committed bag instance")

        # 3. generation claims must match the allocator watermark history. A
        # dispatched generation above the current one could only be forged.
        current_generation = state.sequence.epoch
        for key in self.keys():
            if key.generation > current_generation:
                issues.append(
                    f"dedup key {key.encode()!r} claims generation "
                    f"{key.generation}, beyond the watermark generation "
                    f"{current_generation}"
                )

        # 4. schema boundary semantics. Under the historical v1 rule one bag
        # id could only ever be dispatched once, regardless of generation;
        # therefore an id found under several generations *before* the v2
        # marker would be an unresolvable collision (and could only arise from
        # tampered or mixed-schema data). Same-id distinct generations after
        # the marker are the whole point of v2 and are fine.
        pre_boundary = self._sort_ordinals_before_v2()
        if pre_boundary is None:
            # No v2 marker at all: every sort record is a v1 record.
            pre_boundary = len(state.sort_records)
        seen_before: dict[str, set[int]] = {}
        for ordinal, raw in enumerate(state.dedup_keys, start=1):
            if ordinal > pre_boundary:
                continue
            key = decode_key(raw)
            seen_before.setdefault(key.bag_id, set()).add(key.generation)
        for bag_id, generations in sorted(seen_before.items()):
            if len(generations) > 1:
                issues.append(
                    f"bag {bag_id!r} was dispatched under generations "
                    f"{sorted(generations)} before the v2 dedup key boundary"
                )

        return Reconciliation(
            schema=schema,
            consistent=not issues,
            dedup_entries=len(state.dedup_keys),
            sort_records=len(state.sort_records),
            bag_instances=len(state.bag_instances),
            issues=tuple(issues),
        )

    def _sort_ordinals_before_v2(self) -> int | None:
        """Count of sort records committed before the first v2 schema marker.

        Returns ``None`` when no v2 marker exists.
        """

        from bagsort.model.dedup import MARKER_KIND

        sort_ordinal = 0
        for entry in self._store.entries():
            if entry.kind == MARKER_KIND and int(entry.payload.get("to")) == KEY_SCHEMA_V2:
                return sort_ordinal
            if entry.kind == "sort.record":
                sort_ordinal += 1
        return None
