"""Deduplication gate, key grammar and the cross-checkable admission ledger."""

from .keys import (
    LEGACY_EPOCH,
    SCHEME_V1,
    SCHEME_V2,
    key_for,
    key_for_epoch,
    key_v1,
    key_v2,
)
from .ledger import DedupDecision, DedupLedger

__all__ = [
    "LEGACY_EPOCH",
    "SCHEME_V1",
    "SCHEME_V2",
    "DedupDecision",
    "DedupLedger",
    "key_for",
    "key_for_epoch",
    "key_v1",
    "key_v2",
]
