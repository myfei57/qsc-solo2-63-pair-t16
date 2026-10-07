"""Dedup scheme, admissions and bag generations survive a restart."""

from __future__ import annotations

from bagsort.model.entities import Sequence
from bagsort.sequence.allocator import MAX_SEQUENCE
from bagsort.store.store import Store


def _at_wrap(plant):
    with plant.store.session() as session:
        session.state.sequence = Sequence(epoch=0, next_value=MAX_SEQUENCE - 1)
        session.record("sequence.put", {"epoch": 0, "next": MAX_SEQUENCE - 1})


def test_schemes_admissions_and_generations_round_trip(tmp_path, plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("DUP", flight.id)
    plant.check_in("DUP", flight.id)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=0)
    plant.push_bag("T1-DUP", "T1-CHA", epoch=1)
    before_keys = plant.dedup.admitted_keys()
    before_schemes = [s.as_dict() for s in plant.dedup.history()]

    plant.store.persist()
    reopened = Store.open(plant.store.path)

    assert [s.as_dict() for s in reopened.state().dedup_schemes] == before_schemes
    assert [a.key for a in reopened.state().dedup_admissions] == before_keys
    generations = [(b.id, b.epoch) for b in reopened.state().bag_generations]
    assert ("T1-DUP", 0) in generations and ("T1-DUP", 1) in generations


def test_a_restart_does_not_redeclare_the_cutover(tmp_path, plant, flight):
    _at_wrap(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.check_in("DUP", flight.id)
    plant.check_in("DUP", flight.id)
    declared_once = len(plant.dedup.history())
    plant.store.persist()

    from bagsort.app import Plant
    from bagsort.clock import Clock

    reopened = Plant.build(Store.open(plant.store.path), clock=Clock())
    # Simply observing the rebuilt state must not append another declaration.
    assert len(reopened.dedup.history()) == declared_once


def test_restart_keeps_the_gate_closed_for_a_real_duplicate(tmp_path, plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bag = plant.check_in("ONCE", flight.id)
    plant.push_bag(bag.id, "T1-CHA")
    plant.store.persist()

    from bagsort.app import Plant
    from bagsort.clock import Clock

    reopened = Plant.build(Store.open(plant.store.path), clock=Clock())
    assert reopened.sorter.was_executed(bag.id, bag.epoch) is True
    assert reopened.dedup.reconcile_report()["ok"] is True
