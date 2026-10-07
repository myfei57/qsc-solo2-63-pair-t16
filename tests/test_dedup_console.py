"""Console exposure of the dedup scheme, cross check and rollover review."""

from __future__ import annotations

from bagsort.model.entities import Sequence
from bagsort.sequence.allocator import MAX_SEQUENCE
from tests.support import make_server
from tests.test_console_api import bootstrap, call


def _checkin(server, barcode, flight_id):
    status, bag = call(
        server, "POST", "/api/bags/checkin", {"barcode": barcode, "flight_id": flight_id}
    )
    assert status == 201
    return bag


def test_dedup_status_reconcile_and_keys(server):
    flight_id = bootstrap(server)
    bag = _checkin(server, "7001", flight_id)
    call(server, "POST", "/api/sort", {"bag_id": bag["id"], "chute_id": "T1-CHA"})

    status, status_body = call(server, "GET", "/api/dedup")
    assert status == 200
    assert status_body["scheme"] == "v2"
    assert status_body["reconciliation"]["ok"] is True

    assert call(server, "GET", "/api/dedup/reconcile")[1]["ok"] is True
    assert call(server, "GET", "/api/dedup/keys")[1]["keys"] == ["e0:T1-7001"]


def test_wrap_review_endpoint(server):
    flight_id = bootstrap(server)
    plant = server.plant
    with plant.store.session() as session:
        session.state.sequence = Sequence(epoch=0, next_value=MAX_SEQUENCE - 1)
        session.record("sequence.put", {"epoch": 0, "next": MAX_SEQUENCE - 1})
    old = _checkin(server, "DUP", flight_id)
    wrapped = _checkin(server, "DUP", flight_id)
    call(server, "POST", "/api/sort", {"bag_id": old["id"], "chute_id": "T1-CHA", "epoch": 0})
    call(server, "POST", "/api/sort", {"bag_id": wrapped["id"], "chute_id": "T1-CHA", "epoch": 1})

    status, review = call(server, "GET", "/api/dedup/wrap-review", query={"epoch": ["0"]})
    assert status == 200
    assert review["previous_epoch"] == 0
    pair = next(p for p in review["reused_numbers"] if p["bag_id"] == "T1-DUP")
    assert pair["distinct_keys"] is True


def test_generations_and_epoch_scoped_sort_count(server):
    flight_id = bootstrap(server)
    plant = server.plant
    with plant.store.session() as session:
        session.state.sequence = Sequence(epoch=0, next_value=MAX_SEQUENCE - 1)
        session.record("sequence.put", {"epoch": 0, "next": MAX_SEQUENCE - 1})
    old = _checkin(server, "DUP", flight_id)
    wrapped = _checkin(server, "DUP", flight_id)
    call(server, "POST", "/api/sort", {"bag_id": old["id"], "chute_id": "T1-CHA", "epoch": 0})
    call(server, "POST", "/api/sort", {"bag_id": wrapped["id"], "chute_id": "T1-CHA", "epoch": 1})

    status, gens = call(server, "GET", "/api/bags/T1-DUP/generations")
    assert status == 200
    assert sorted(g["epoch"] for g in gens["generations"]) == [0, 1]

    _, all_counts = call(server, "GET", "/api/bags/T1-DUP/sort-count")
    assert all_counts["count"] == 2
    _, one_epoch = call(server, "GET", "/api/bags/T1-DUP/sort-count", query={"epoch": ["1"]})
    assert one_epoch["count"] == 1
