"""Historical queries, wrap-point review and pre-upgrade compatibility."""

from __future__ import annotations

from bagsort.model.dedup import KEY_SCHEMA_V1, KEY_SCHEMA_V2
from bagsort.model.entities import Sequence
from bagsort.sequence.allocator import MAX_SEQUENCE
from bagsort.store.store import Store
from tests.support import check_in


def _at_wrap(plant, *, last_next=MAX_SEQUENCE):
    with plant.store.session() as session:
        session.state.sequence = Sequence(epoch=0, next_value=last_next)
        session.record("sequence.put", {"epoch": 0, "next": last_next})


def test_generations_of_an_id_remain_queryable_after_a_wrap(plant, flight):
    old = check_in(plant, flight.id, 1, prefix="BAG")[0]
    _at_wrap(plant)
    new = plant.check_in(old.barcode, flight.id)

    generations = plant.reads.generations_of(old.id)
    assert generations == [0, 1]
    instances = plant.bag_generations(old.id)
    assert [bag["epoch"] for bag in instances] == [0, 1]
    assert [bag["sequence"] for bag in instances] == [old.sequence, new.sequence]
    # The live bag map only shows the newest generation; the ledger has both.
    assert plant.bag(old.id).epoch == 1
    assert len(plant.store.state().bag_instances) == 2


def test_wrap_point_lists_bags_on_both_sides_for_piecewise_review(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    before = check_in(plant, flight.id, 3, prefix="OLD")
    _at_wrap(plant, last_next=MAX_SEQUENCE - 1)
    edge = check_in(plant, flight.id, 2, prefix="EDGE")
    after = []
    for bag in [*edge, *check_in(plant, flight.id, 2, prefix="NEW")]:
        after.append(bag)
    review = plant.wrap_point(window=2)
    epochs_sequences = [(bag["epoch"], bag["sequence"]) for bag in review]
    # Last two committed bags of the old generation, then the first two new.
    assert epochs_sequences == [(0, 3), (0, MAX_SEQUENCE), (1, 1), (1, 2)]
    assert len({bag["id"] for bag in review}) == len(review)


def test_recheck_distinguishes_two_generations_of_the_same_id(plant, flight):
    old = check_in(plant, flight.id, 1, prefix="BAG")[0]
    plant.enqueue_recheck(old.id)
    _at_wrap(plant)
    new = plant.check_in(old.barcode, flight.id)
    plant.enqueue_recheck(new.id)

    tokens = plant.store.state().recheck_queue
    assert tokens == [old.id, f"{new.id}#1"]
    dequeued = [plant.process_recheck() for _ in range(2)]
    assert [(bag.id, bag.epoch) for bag in dequeued] == [(old.id, 0), (new.id, 1)]


def test_recheck_queue_survives_restart_with_two_generations(plant, flight):
    old = check_in(plant, flight.id, 1, prefix="BAG")[0]
    plant.enqueue_recheck(old.id)
    _at_wrap(plant)
    new = plant.check_in(old.barcode, flight.id)
    plant.enqueue_recheck(new.id)
    plant.store.persist()

    reopened = Store.open(plant.store.path)
    from bagsort.app import Plant

    plant2 = Plant.build(reopened)
    items = [(bag.id, bag.epoch) for bag in plant2.recheck.items()]
    assert items == [(old.id, 0), (new.id, 1)]


def test_pre_upgrade_journal_reads_as_v1_and_reconciles(tmp_path):
    # A journal written with only v1-era commands (no marker) must replay as
    # schema v1, and its generation 0 records must stay reachable.
    path = tmp_path / "legacy.json"
    store = Store(str(path))
    with store.session() as session:
        session.record(
            "flight.put",
            {"flight": {"id": "T1-CA1", "code": "CA1", "state": "open"}},
        )
        session.record(
            "bag.put",
            {"bag": {"id": "T1-1", "barcode": "1", "sequence": 1}},
        )
        session.record(
            "sort.record",
            {"record": {"bag_id": "T1-1", "flight_id": "T1-CA1",
                        "chute_id": "T1-CHA", "sequence": 1, "epoch": 0, "at": ""}},
        )
    store.persist()

    reopened = Store.open(str(path))
    from bagsort.app import Plant

    plant = Plant.build(reopened)
    assert plant.dedup.current_schema() == KEY_SCHEMA_V1
    assert reopened.state().dedup_keys == ["T1-1#0"]
    report = plant.dedup.reconcile()
    assert report.consistent is True
    # Old behaviour is preserved for the historical generation.
    assert plant.sorter.was_executed("T1-1", 0) is True


def test_pre_upgrade_collision_is_reported_not_silently_accepted(tmp_path):
    # Mixed data: the same id dispatched under two generations with no v2
    # boundary between them is unresolvable under v1 and must be flagged.
    path = tmp_path / "mixed.json"
    store = Store(str(path))
    with store.session() as session:
        session.record(
            "bag.put",
            {"bag": {"id": "T1-1", "barcode": "1", "sequence": 1, "epoch": 0}},
        )
        session.record(
            "sort.record",
            {"record": {"bag_id": "T1-1", "flight_id": "F", "chute_id": "C",
                        "sequence": 1, "epoch": 0, "at": ""}},
        )
        session.record(
            "bag.put",
            {"bag": {"id": "T1-1", "barcode": "1", "sequence": 1, "epoch": 1}},
        )
        session.record(
            "sort.record",
            {"record": {"bag_id": "T1-1", "flight_id": "F", "chute_id": "C",
                        "sequence": 1, "epoch": 1, "at": ""}},
        )
    from bagsort.app import Plant

    plant = Plant.build(store)
    report = plant.dedup.reconcile()
    assert report.schema == KEY_SCHEMA_V1
    assert report.consistent is False
    assert any("before the v2 dedup key boundary" in issue for issue in report.issues)
