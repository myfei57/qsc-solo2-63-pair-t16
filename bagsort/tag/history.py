"""Projection of committed reads, rebuilt from the journal."""

from __future__ import annotations

from bagsort.model.entities import Bag
from bagsort.store.store import Store


class ReadHistory:
    """Which barcodes have been committed, and how many epoch wraps happened."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def committed_bags(self) -> list[Bag]:
        state = self._store.state()
        return [state.bags[key] for key in sorted(state.bags)]

    def generations(self) -> list[Bag]:
        """One record per committed (baggage number, sequence generation)."""

        return list(self._store.state().bag_generations)

    def generation(self, bag_id: str, epoch: int) -> Bag | None:
        return next(
            (
                bag
                for bag in self._store.state().bag_generations
                if bag.id == bag_id and bag.epoch == epoch
            ),
            None,
        )

    def count(self) -> int:
        return len(self._store.state().bags)

    def epochs(self) -> list[int]:
        """Distinct epochs that have produced committed readings."""

        return sorted({bag.epoch for bag in self._store.state().bags.values()})
