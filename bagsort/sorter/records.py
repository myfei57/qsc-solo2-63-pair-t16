"""Read-only projection over the committed sort records.

The dedup question dispatch asks is "has *this* bag already been sorted?" A bag
is a ``(bag id, generation)`` pair: the id alone collides with the previous
generation once the allocating sequence has wrapped. Membership therefore
reads the materialised composite dedup set (one encoded key per sort record).

The bag-id-only queries are kept for the console's historical views. They are
deliberately cross-generation, so an id reused after a wrap shows every
generation it was sorted under instead of silently dropping the old ones.
"""

from __future__ import annotations

from bagsort.model.entities import SortRecord
from bagsort.sorter.dedup_book import DedupLog
from bagsort.store.store import Store


class SortLog:
    """Answers the questions dispatch needs before it writes a new record."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._dedup = DedupLog(store)

    @property
    def dedup(self) -> DedupLog:
        return self._dedup

    def records(self) -> list[SortRecord]:
        return list(self._store.state().sort_records)

    def records_for(self, bag_id: str, generation: int | None = None) -> list[SortRecord]:
        """Sort records for an id, optionally narrowed to one generation."""

        return [
            record
            for record in self._store.state().sort_records
            if record.bag_id == bag_id
            and (generation is None or record.epoch == generation)
        ]

    def was_executed(self, bag_id: str, generation: int | None = None) -> bool:
        """Composite dedup check when a generation is given.

        Without a generation this is the legacy cross-generation view and
        counts any record for the id; dispatch always passes one.
        """

        if generation is None:
            return self.count_for(bag_id) > 0
        return self._dedup.contains(bag_id, generation)

    def count_for(self, bag_id: str) -> int:
        return sum(1 for record in self._store.state().sort_records if record.bag_id == bag_id)

    def last_chute(self, bag_id: str) -> str:
        last = ""
        for record in self._store.state().sort_records:
            if record.bag_id == bag_id:
                last = record.chute_id
        return last

    def by_chute(self, flight_id: str | None = None) -> dict[str, int]:
        summary: dict[str, int] = {}
        for record in self._store.state().sort_records:
            if flight_id is not None and record.flight_id != flight_id:
                continue
            summary[record.chute_id] = summary.get(record.chute_id, 0) + 1
        return dict(sorted(summary.items()))
