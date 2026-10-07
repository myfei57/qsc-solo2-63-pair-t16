"""Dispatch: the step that turns a committed reading into a sort record.

Dispatch refuses an uncommitted reading outright, and it refuses a bag whose
deduplication key has already been admitted. Both refusals are the contracts
other subsystems rely on: the desk must commit before routing, and a retried
induction must not create a second sort record.

The dedup key is baggage number *plus* sequence generation (see
:mod:`bagsort.dedup`), so after a sequence wrap a new bag that reuses a number
is a different key and sorts normally, while a real re-induction of the same
bag in the same generation collides and is refused exactly once.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.dedup.ledger import DedupLedger
from bagsort.errors import ConflictError
from bagsort.model.entities import Bag, Reading, SortRecord
from bagsort.model.enums import BagState
from bagsort.sequence.verdict import VerdictTracker
from bagsort.store.store import Store
from bagsort.chute.assigner import ChuteAssigner
from bagsort.flight.mapping import BagMapping
from .records import SortLog
from .route import RouteDetail


class Sorter:
    """Routes committed bags to their flight's current chute."""

    def __init__(
        self,
        store: Store,
        mapping: BagMapping,
        chutes: ChuteAssigner,
        clock: Clock,
        verdict: VerdictTracker | None = None,
        dedup: DedupLedger | None = None,
    ) -> None:
        self._store = store
        self._mapping = mapping
        self._chutes = chutes
        self._clock = clock
        self._verdict = verdict or VerdictTracker()
        self._dedup = dedup or DedupLedger(store, clock)
        self._log = SortLog(store)

    @property
    def log(self) -> SortLog:
        return self._log

    @property
    def dedup(self) -> DedupLedger:
        return self._dedup

    def route(self, bag_id: str) -> RouteDetail:
        flight_id = self._mapping.flight_of(bag_id)
        assignment = self._chutes.require(flight_id)
        return RouteDetail(
            bag_id=bag_id,
            flight_id=flight_id,
            chute_id=assignment.chute_id,
            generation=assignment.generation,
        )

    def dispatch(self, bag: Bag, reading: Reading) -> SortRecord:
        if not reading.committed:
            raise ConflictError(f"barcode read for {bag.barcode!r} is not committed")
        # Judge by number plus generation before opening the transaction, so a
        # refused re-induction buffers no commands at all.
        decision = self._dedup.decision_for(bag.id, bag.epoch)
        if not decision.allowed:
            raise ConflictError(
                f"bag {bag.id!r} in epoch {bag.epoch} has already been sorted "
                f"under dedup key {decision.key!r}"
            )
        detail = self.route(bag.id)
        record = SortRecord(
            bag_id=bag.id,
            flight_id=detail.flight_id,
            chute_id=detail.chute_id,
            sequence=bag.sequence,
            epoch=bag.epoch,
            at=self._clock.now_iso(),
        )
        updated = bag.evolved(chute_id=detail.chute_id, state=BagState.SORTED)
        with self._store.session() as session:
            session.state.sort_records.append(record)
            session.record(
                "sort.record", {"record": record.as_dict()}, self._clock.now_iso()
            )
            # Admit the key in the same transaction, immediately after the
            # record it authorises, so the two journals line up for reconciliation.
            self._dedup.record_admission(session, bag.id, bag.epoch, record.at)
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "sorter.dispatched", "delta": 1}, self._clock.now_iso()
            )
        return record

    def divert(self, bag: Bag) -> Bag:
        updated = bag.evolved(state=BagState.DIVERTED)
        with self._store.session() as session:
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "sorter.diverted", "delta": 1}, self._clock.now_iso()
            )
        return updated

    def verdict(self, epoch: int, sequence: int):
        """Classify an observed pair and advance the mark when it is fresh."""

        return self._verdict.observe(epoch, sequence)

    def is_fresh(self, epoch: int, sequence: int) -> bool:
        return self._verdict.is_fresh(epoch, sequence)

    def high_water(self) -> tuple[int, int]:
        return self._verdict.high_water()

    def was_executed(self, bag_id: str, epoch: int | None = None) -> bool:
        """Whether a bag was sorted.

        With no generation given this answers across all generations (the
        legacy question "have I ever seen this number?"); with an epoch it asks
        the precise question the dedup gate answers.
        """

        if epoch is None:
            return self._log.was_executed(bag_id)
        return not self._dedup.is_allowed(bag_id, epoch)

    def records(self) -> list[SortRecord]:
        return self._log.records()

    def assign_chute(self, flight_id: str, chute_id: str):
        """Convenience for the injection belt, which sets the lane then dispatches."""

        return self._chutes.assign(flight_id, chute_id)
