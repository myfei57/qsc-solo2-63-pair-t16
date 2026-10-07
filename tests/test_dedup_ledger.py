"""The admission ledger: the gate, the cross check and rollover review."""

from __future__ import annotations

import pytest

from bagsort.dedup.keys import SCHEME_V2
from bagsort.model.entities import Sequence
from bagsort.sequence.allocator import MAX_SEQUENCE
from tests.support import check_in


def _at_wrap(plant):
    with plant.store.session() as session:
        session.state.sequence = Sequence(epoch=0, next_value=MAX_SEQUENCE - 1)
        session.record("sequence.put", {"epoch": 0, "next": MAX_SEQUENCE - 1})


def test_a_wrapped_bag_reusing_a_number_sorts_as_its_own_piece(plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    old = plant.check_in("DUP", flight.id)          # seq 1000, epoch 0
    wrapped_first = plant.check_in("NEW1", flight.id)  # seq 1, epoch 1
    wrapped_dup = plant.check_in("DUP", flight.id)  # seq 2, epoch 1, same number

    assert (old.epoch, old.sequence) == (0, MAX_SEQUENCE)
    assert (wrapped_dup.epoch, wrapped_dup.sequence) == (1, 2)

    plant.push_bag(old.id, "T1-CHA", epoch=0)
    plant.push_bag(wrapped_first.id, "T1-CHA")
    # This used to be refused as a copy of the epoch-0 bag; now it sorts.
    record = plant.push_bag(wrapped_dup.id, "T1-CHA", epoch=1)
    assert record.epoch == 1
    assert plant.sorter.log.count_for("T1-DUP") == 2
    assert plant.sorter.log.count_for_epoch("T1-DUP", 1) == 1


def test_a_real_re_induction_in_one_generation_is_still_refused_once(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1, prefix="ONE")[0]
    plant.push_bag(bag.id, "T1-CHA")
    assert plant.retry_bag(bag.id, bag.epoch) is False
    assert plant.sorter.log.count_for_epoch(bag.id, bag.epoch) == 1


def test_both_generations_around_a_wrap_are_independently_retryable(plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("DUP", flight.id)
    plant.check_in("DUP", flight.id)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=0)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=1)
    # Same number, different generation: each is blocked only in its own epoch.
    assert plant.sorter.was_executed("T1-DUP", 0) is True
    assert plant.sorter.was_executed("T1-DUP", 1) is True
    assert plant.retry_bag("T1-DUP", 0) is False
    assert plant.retry_bag("T1-DUP", 1) is False


def test_admitted_keys_carry_the_epoch_after_a_wrap(plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("DUP", flight.id)
    plant.check_in("DUP", flight.id)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=0)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=1)
    keys = plant.dedup.admitted_keys()
    assert "e0:T1-DUP" in keys
    assert "e1:T1-DUP" in keys


def test_reconcile_cross_checks_admissions_against_sort_records(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    check_in(plant, flight.id, 3)
    for bag_id in sorted(plant.store.state().bags):
        plant.push_bag(bag_id, "T1-CHA")
    report = plant.dedup.reconcile_report()
    assert report["ok"] is True
    assert report["admissions"] == 3
    assert report["widened_records"] == 3
    assert report["legacy_records"] == 0
    assert report["mismatches"] == []


def test_reconcile_flags_an_admission_that_does_not_line_up(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1)[0]
    plant.push_bag(bag.id, "T1-CHA")
    # Tamper an admission so its key no longer renders from its own fields.
    state = plant.store.state()
    bogus = state.dedup_admissions[0]
    state.dedup_admissions[0] = bogus.__class__(
        scheme=bogus.scheme, key="tampered", bag_id=bogus.bag_id,
        epoch=bogus.epoch, at=bogus.at,
    )
    report = plant.dedup.reconcile_report()
    assert report["ok"] is False
    assert report["mismatches"]


def test_wrap_window_and_reused_pairs(plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("DUP", flight.id)
    plant.check_in("OTHER", flight.id)
    plant.check_in("DUP", flight.id)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=0)
    plant.push_bag("T1-OTHER", "T1-CHA", epoch=1)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=1)

    review = plant.wrap_review(0, limit=5)
    keys = {(row["epoch"], row["bag_id"]): row["key"] for row in review["window"]}
    assert keys[(0, "T1-DUP")] == "e0:T1-DUP"
    assert keys[(1, "T1-DUP")] == "e1:T1-DUP"

    pairs = review["reused_numbers"]
    dup = next(pair for pair in pairs if pair["bag_id"] == "T1-DUP")
    assert dup["distinct_keys"] is True


def test_born_on_v2_store_keys_generation_zero_without_an_audit_change(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1)[0]
    plant.push_bag(bag.id, "T1-CHA")
    scheme = plant.dedup.current()
    assert scheme.scheme == SCHEME_V2 and scheme.cutoff_epoch == 0
    assert plant.audit.by_kind().get("dedup_scheme") is None


def test_a_wrap_is_audit_linked(plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("DUP", flight.id)
    plant.check_in("DUP", flight.id)
    kinds = plant.audit.by_kind()
    assert kinds.get("epoch_wrap") == 1
    assert plant.audit.verify() is True


def test_legacy_snapshot_seals_epoch_zero_and_queries_its_history(plant, flight):
    # Hand-write a pre-upgrade journal: a sorted epoch-0 bag with no scheme.
    at = "2024-01-01T00:00:00Z"
    with plant.store.session() as session:
        session.record(
            "bag.put",
            {"bag": {"id": "T1-OLD", "barcode": "OLD", "sequence": 1, "epoch": 0,
                     "flight_id": flight.id, "chute_id": "T1-CHA", "state": "sorted"}},
            at,
        )
        session.record(
            "sort.record",
            {"record": {"bag_id": "T1-OLD", "flight_id": flight.id, "chute_id": "T1-CHA",
                         "sequence": 1, "epoch": 0, "at": at}},
            at,
        )
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("NEWB", flight.id)
    scheme = plant.dedup.current()
    assert scheme.scheme == SCHEME_V2 and scheme.cutoff_epoch == 1
    # The old record is still queryable and is treated as legacy, not a mismatch.
    assert plant.sorter.log.count_for("T1-OLD") == 1
    report = plant.dedup.reconcile_report()
    assert report["legacy_records"] == 1
    assert report["ok"] is True
    assert plant.reads.generation("T1-OLD", 0).state.value == "sorted"
