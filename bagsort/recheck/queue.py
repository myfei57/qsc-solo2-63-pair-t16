"""Recheck queue: strictly first in, first out.

Bags are intercepted in the order they arrive and processed in that same order.
The queue is part of the committed document, so a restart resumes the backlog
instead of silently reordering it.

Queue entries are encoded ``(bag id, generation)`` tokens. A bag id is derived
from the barcode and is reused by a later generation once the sequence wraps,
so a bare id cannot tell a queued old-generation bag from a new one. Tokens
written before that change are bare ids; they decode to generation 0 and keep
reading.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.model.dedup import DedupKey, encode_key
from bagsort.model.entities import Bag
from bagsort.model.enums import BagState
from bagsort.model.errors import unknown_bag
from bagsort.store.store import Store


class RecheckQueue:
    """FIFO backlog of bags waiting for a second look."""

    def __init__(self, store: Store, book: object, clock: Clock) -> None:
        self._store = store
        self._book = book
        self._clock = clock

    @staticmethod
    def token_for(bag: Bag) -> str:
        # Generation 0 keeps the historical bare-id token so pre-wrap queues
        # and snapshots stay byte-identical; later generations are qualified.
        if bag.epoch == 0:
            return bag.id
        return encode_key(bag.id, bag.epoch)

    def _resolve(self, token: str) -> Bag:
        key = DedupKey.decode(token)
        bag = self._store.state().bags.get(key.bag_id)
        if bag is not None and bag.epoch == key.generation:
            return bag
        historical = self._historical_bag(key.bag_id, key.generation)
        if historical is not None:
            return historical
        # Bare legacy tokens always mean the generation-0 bag.
        if bag is not None and key.generation == 0:
            return bag
        raise unknown_bag(encode_key(key.bag_id, key.generation))

    def _historical_bag(self, bag_id: str, generation: int) -> Bag | None:
        found: Bag | None = None
        for entry in self._store.entries():
            if entry.kind != "bag.put":
                continue
            raw = entry.payload.get("bag", {})
            if raw.get("id") == bag_id and int(raw.get("epoch", 0)) == generation:
                found = Bag.from_dict(raw)
        return found

    def enqueue(self, bag: Bag) -> int:
        updated = bag.evolved(state=BagState.RECHECKED)
        token = self.token_for(bag)
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.bags[updated.id] = updated
            if token not in session.state.recheck_queue:
                session.state.recheck_queue.append(token)
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.record("recheck.enqueue", {"bag_id": token}, at)
            session.record("counter.add", {"name": "recheck.enqueued", "delta": 1}, at)
        return len(self._store.state().recheck_queue)

    def dequeue(self) -> Bag | None:
        state = self._store.state()
        if not state.recheck_queue:
            return None
        token = state.recheck_queue[0]
        bag = self._resolve(token)
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.recheck_queue = [
                item for item in session.state.recheck_queue if item != token
            ]
            session.record("recheck.dequeue", {"bag_id": token}, at)
            session.record("counter.add", {"name": "recheck.processed", "delta": 1}, at)
        return bag

    def peek(self) -> Bag | None:
        state = self._store.state()
        if not state.recheck_queue:
            return None
        return self._resolve(state.recheck_queue[0])

    def items(self) -> list[Bag]:
        state = self._store.state()
        resolved: list[Bag] = []
        for token in state.recheck_queue:
            try:
                resolved.append(self._resolve(token))
            except Exception:
                continue
        return resolved

    def length(self) -> int:
        return len(self._store.state().recheck_queue)

    def clear(self) -> int:
        dropped = len(self._store.state().recheck_queue)
        with self._store.session() as session:
            session.state.recheck_queue = []
            session.record("recheck.clear", {}, self._clock.now_iso())
        return dropped

    def deadline(self, flight_id: str) -> str:
        return self._book.deadline(flight_id)
