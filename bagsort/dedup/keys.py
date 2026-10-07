"""Deduplication: deciding whether a bag may be sorted a second time.

Before epoch rollover was taken into account, the gate keyed only on the
baggage number (the namespaced bag id). After a sequence wrap the numbers
start again at 1, so a genuinely new bag that happened to reuse a number was
mistaken for a retry of the old bag and was refused. The key is therefore
versioned:

* ``v1`` — the legacy grammar ``bag_id``. Every record judged before the key
  change keeps this key, so the old history stays queryable exactly as it was
  recorded.
* ``v2`` — the widened grammar ``epoch:bag_id``. The sequence generation is part
  of the key, so a wrapped bag in a new epoch never collides with an old one,
  while a real re-induction of the same (epoch, bag) still collides and is
  refused once.

The two grammars share one ordered admission list rather than living in
separate per-epoch spaces. This keeps a single cross-check between the
admission set, the sequence generations and the sort records, and it keeps a
legacy snapshot replayable without a migration: a ``v1`` key is a valid entry
forever. The cost, and the documented compatibility boundary, is that a ``v1``
admission can only ever speak for the epoch it was admitted in — see
:func:`key_for_epoch` and :data:`LEGACY_EPOCH`.
"""

from __future__ import annotations

SCHEME_V1 = "v1"
SCHEME_V2 = "v2"

#: Legacy records predate epoch-scoped judgments; they are only ever consulted
#: for generation 0, which is the only epoch a v1 plant could produce.
LEGACY_EPOCH = 0


def key_v1(bag_id: str) -> str:
    """The legacy key: the baggage number alone."""

    return bag_id


def key_v2(bag_id: str, epoch: int) -> str:
    """The widened key: sequence generation plus baggage number."""

    return f"e{int(epoch)}:{bag_id}"


def key_for(scheme: str, bag_id: str, epoch: int) -> str:
    """Render a key under the named grammar."""

    if scheme == SCHEME_V1:
        return key_v1(bag_id)
    if scheme == SCHEME_V2:
        return key_v2(bag_id, epoch)
    raise ValueError(f"unknown dedup scheme {scheme!r}")


def key_for_epoch(
    bag_id: str,
    epoch: int,
    active_scheme: str,
    cutoff_epoch: int,
) -> str:
    """The key a (bag, epoch) pair is judged under, given a declared boundary.

    A record in a generation below ``cutoff_epoch`` belongs to history judged
    under the legacy grammar and is looked up there; everything at or above the
    boundary uses the currently active grammar. This is what lets a sealed v1
    history still answer "was this exact old bag already sorted?" without a v1
    key shadowing a wrapped v2 bag.
    """

    if epoch < cutoff_epoch and active_scheme == SCHEME_V2:
        return key_v1(bag_id)
    return key_for(active_scheme, bag_id, epoch)
