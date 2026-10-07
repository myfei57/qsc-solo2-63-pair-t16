"""Composite (bag id, generation) dispatch dedup across a sequence wrap.

The bug these tests pin: dedup used to key on the bag id alone, so the first
new-generation bag reusing an id after a wrap was refused as a "duplicate" of
the old-generation bag. The key is now the composite pair, while a genuine
retry within one generation still only books once.
"""

from __future__ import annotations

import pytest

from bagsort.model.dedup import (
    KEY_SCHEMA_V1,
    KEY_SCHEMA_V2,
    MARKER_KIND,
    DedupKey,
    decode_key,
    encode_key,
)
from bagsort.model.entities import Sequence, SortRecord
from bagsort.sequence.allocator import MAX_SEQUENCE
from tests.support import check_in


def _force_to_wrap(plant) -> None:
    with plant.store.session() as session:
        session.state.sequence = Sequence(epoch=0, next_value=MAX_SEQUENCE)
        session.record("sequence.put", {"epoch": 0, "next": MAX_SEQUENCE})


def _sort(plant, bag) -> SortRecord:
    plant.assign_chute(bag.flight_id, "T1-CHA")
    return plant.push_bag(bag.id, "T1-CHA")


def test_key_round_trip():
    assert encode_key("T1-9", 3) == "T1-9#3"
    assert decode_key("T1-9#3") == DedupKey(bag_id="T1-9", generation=3)
    # A bare historical token decodes as the implicit generation 0 key.
    assert decode_key("T1-9") == DedupKey(bag_id="T1-9", generation=0)


def test_same_generation_retry_is_still_refused(plant, flight):
    bag = check_in(plant, flight.id, 1)[0]
    plant.assign_chute(flight.id, "T1-CHA")
    plant.push_bag(bag.id, "T1-CHA")
    assert plant.sorter.log.count_for(bag.id) == 1
    assert plant.retry_bag(bag.id) is False
    assert plant.sorter.log.count_for(bag.id) == 1


def test_post_wrap_bag_with_reused_id_sorts_normally(plant, flight):
    old = check_in(plant, flight.id, 1, prefix="BAG")[0]
    _sort(plant, old)
    _force_to_wrap(plant)

    new = plant.check_in(old.barcode, flight.id)
    assert new.id == old.id
    assert (new.epoch, new.sequence) == (1, 1)

    record = _sort(plant, new)
    assert (record.epoch, record.sequence) == (1, 1)
    # The id now has exactly one sort record per generation.
    assert plant.sorter.log.count_for(new.id) == 2
    assert plant.sorter.log.was_executed(new.id, 0) is True
    assert plant.sorter.log.was_executed(new.id, 1) is True
    # Retrying the new-generation bag still books once.
    assert plant.retry_bag(new.id) is False
    assert plant.sorter.log.count_for(new.id) == 2


def test_wrap_marker_lands_before_the_first_new_generation_bag(plant, flight):
    first = check_in(plant, flight.id, 1, prefix="BAG")[0]
    _sort(plant, first)
    assert plant.dedup.current_schema() == KEY_SCHEMA_V1
    _force_to_wrap(plant)
    new = plant.check_in(first.barcode, flight.id)
    _sort(plant, new)
    assert plant.dedup.current_schema() == KEY_SCHEMA_V2

    kinds = [entry.kind for entry in plant.store.entries()]
    marker = kinds.index(MARKER_KIND)
    first_gen1_sort = next(
        index
        for index, entry in enumerate(plant.store.entries())
        if entry.kind == "sort.record"
        and SortRecord.from_dict(entry.payload["record"]).epoch == 1
    )
    assert marker < first_gen1_sort


def test_schema_upgrade_is_idempotent_and_audited(plant):
    change = plant.upgrade_dedup_schema()
    assert change["changed"] is True
    assert change["from"] == KEY_SCHEMA_V1
    assert change["to"] == KEY_SCHEMA_V2
    again = plant.upgrade_dedup_schema()
    assert again["changed"] is False
    kinds = plant.audit.by_kind()
    assert kinds.get("dedup_schema") == 1
    assert plant.audit.verify() is True
    entry = next(e for e in plant.audit.entries() if e.kind == "dedup_schema")
    assert entry.detail["to_schema"] == KEY_SCHEMA_V2
    assert entry.subject == "dedup-key"


def test_manual_upgrade_before_a_wrap_also_works(plant, flight):
    plant.upgrade_dedup_schema()
    bag = check_in(plant, flight.id, 1)[0]
    _sort(plant, bag)
    _force_to_wrap(plant)
    new = plant.check_in(bag.barcode, flight.id)
    _sort(plant, new)
    assert plant.reconcile_dedup()["consistent"] is True


def test_reconcile_matches_set_ledger_and_records(plant, flight):
    old = check_in(plant, flight.id, 1)[0]
    _sort(plant, old)
    _force_to_wrap(plant)
    new = plant.check_in(old.barcode, flight.id)
    _sort(plant, new)

    report = plant.reconcile_dedup()
    assert report["consistent"] is True
    assert report["schema"] == KEY_SCHEMA_V2
    assert report["dedup_entries"] == report["sort_records"] == 2
    # The materialised dedup set really is keyed on the composite.
    assert plant.store.state().dedup_keys == [
        encode_key(old.id, 0),
        encode_key(new.id, 1),
    ]


def test_reconcile_flags_a_forged_duplicate(plant, flight):
    old = check_in(plant, flight.id, 1)[0]
    _sort(plant, old)
    _force_to_wrap(plant)
    new = plant.check_in(old.barcode, flight.id)
    _sort(plant, new)
    # Tamper the set so one key appears twice without a matching record.
    state = plant.store.state()
    state.dedup_keys.append(encode_key(new.id, 1))
    report = plant.dedup.reconcile()
    assert report.consistent is False
    assert any("more than once" in issue for issue in report.issues)
