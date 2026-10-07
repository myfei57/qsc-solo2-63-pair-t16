"""Projection of committed reads, rebuilt from the journal.

The materialised ``bags`` map holds the newest record of each bag id. Because
an id is reissued to a new generation after a sequence wrap, that map alone
cannot answer "every bag that ever carried this id" or "what sat around the
wrap point". Those questions are answered from the append-only
``bag_instances`` ledger and from the journal.
"""

from __future__ import annotations

from bagsort.model.dedup import DedupKey
from bagsort.model.entities import Bag
from bagsort.store.store import Store


class ReadHistory:
    """Which barcodes have been committed, and how many epoch wraps happened."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def committed_bags(self) -> list[Bag]:
        state = self._store.state()
        return [state.bags[key] for key in sorted(state.bags)]

    def count(self) -> int:
        return len(self._store.state().bags)

    def epochs(self) -> list[int]:
        """Distinct epochs that have produced committed readings."""

        return sorted({bag.epoch for bag in self._store.state().bags.values()})

    # -- generation ledger -------------------------------------------------

    def generations_of(self, bag_id: str) -> list[int]:
        """Every generation under which an id was committed, ascending."""

        prefix = f"{bag_id}#"
        return sorted(
            {
                DedupKey.decode(raw).generation
                for raw in self._store.state().bag_instances
                if raw.startswith(prefix)
            }
        )

    def instance(self, bag_id: str, generation: int) -> Bag | None:
        """Reconstruct one committed bag generation from the journal.

        Returns the final state that generation was left in, since a bag is
        written several times within its own life.
        """

        found: Bag | None = None
        for entry in self._store.entries():
            if entry.kind != "bag.put":
                continue
            raw = entry.payload.get("bag", {})
            if raw.get("id") == bag_id and int(raw.get("epoch", 0)) == generation:
                found = Bag.from_dict(raw)
        return found

    def instances_of(self, bag_id: str) -> list[Bag]:
        """Every bag generation that ever carried an id, generation ascending."""

        bags = [
            bag
            for generation in self.generations_of(bag_id)
            if (bag := self.instance(bag_id, generation)) is not None
        ]
        return sorted(bags, key=lambda bag: (bag.epoch, bag.sequence))

    def wrap_point(self, window: int = 5) -> list[Bag]:
        """The bags committed around the latest generation boundary.

        Returns up to ``window`` bags of the previous generation ending at the
        wrap and ``window`` bags of the new generation starting after it, in
        commit order, so an operator can re-check the boundary piece by piece.
        """

        sequence = self._store.state().sequence
        if sequence.epoch <= 0:
            return []
        births = self._births_in_order()
        boundary = next(
            (index for index, bag in enumerate(births) if bag.epoch >= sequence.epoch),
            len(births),
        )
        previous = [bag for bag in births[:boundary] if bag.epoch == sequence.epoch - 1][-window:]
        new = [bag for bag in births[boundary:] if bag.epoch == sequence.epoch][:window]
        return previous + new

    def _births_in_order(self) -> list[Bag]:
        """The first committed state of every (id, generation), commit ordered."""

        seen: set[str] = set()
        ordered: list[Bag] = []
        for entry in self._store.entries():
            if entry.kind != "bag.put":
                continue
            bag = Bag.from_dict(entry.payload["bag"])
            token = f"{bag.id}#{bag.epoch}"
            if token in seen:
                continue
            seen.add(token)
            ordered.append(bag)
        return ordered
