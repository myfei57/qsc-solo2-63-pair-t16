"""The dedup key grammar and its versioned compatibility boundary."""

from __future__ import annotations

import pytest

from bagsort.dedup.keys import (
    SCHEME_V1,
    SCHEME_V2,
    key_for,
    key_for_epoch,
    key_v1,
    key_v2,
)


def test_v1_key_is_the_baggage_number_alone():
    assert key_v1("T1-BAG1") == "T1-BAG1"


def test_v2_key_is_generation_plus_baggage_number():
    assert key_v2("T1-BAG1", 0) == "e0:T1-BAG1"
    assert key_v2("T1-BAG1", 1) == "e1:T1-BAG1"


def test_same_number_in_a_wrapped_epoch_is_a_different_v2_key():
    old = key_v2("T1-BAG1", 0)
    wrapped = key_v2("T1-BAG1", 1)
    assert old != wrapped


def test_same_number_in_the_same_epoch_is_the_same_key():
    first = key_v2("T1-BAG1", 1)
    retried = key_v2("T1-BAG1", 1)
    assert first == retried


def test_key_for_dispatch_dispatches_by_scheme():
    assert key_for(SCHEME_V1, "T1-B", 7) == "T1-B"
    assert key_for(SCHEME_V2, "T1-B", 7) == "e7:T1-B"


def test_key_for_rejects_an_unknown_scheme():
    with pytest.raises(ValueError):
        key_for("v9", "T1-B", 0)


def test_key_for_epoch_keeps_sealed_history_on_legacy_keys():
    # Cutover declared at epoch 1: generation 0 stays on v1, generation 1 on v2.
    assert key_for_epoch("T1-B", 0, SCHEME_V2, cutoff_epoch=1) == "T1-B"
    assert key_for_epoch("T1-B", 1, SCHEME_V2, cutoff_epoch=1) == "e1:T1-B"
    assert key_for_epoch("T1-B", 2, SCHEME_V2, cutoff_epoch=1) == "e2:T1-B"


def test_a_store_born_on_v2_keys_generation_zero_with_the_new_grammar():
    assert key_for_epoch("T1-B", 0, SCHEME_V2, cutoff_epoch=0) == "e0:T1-B"
