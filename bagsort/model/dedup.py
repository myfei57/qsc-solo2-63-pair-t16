"""Deduplication keys for dispatch.

The dispatch dedup rule used to key on the bag id alone. A bag id is derived
from the barcode, so once the allocating sequence wraps a new generation of
bags receives the very same ids again; the first new-generation bag then
looked like a duplicate of the old-generation bag carrying that id and was
refused.

The key is now the composite ``(bag id, generation)``. Two key schema
generations are recognised so old journals stay readable:

``KEY_SCHEMA_V1``
    Historical schema: the bag id alone. Pre-change journals only ever
    committed generation 0 under this schema, so the composite index keeps
    their entries in the ``(id, 0)`` slot and every old lookup still resolves.

``KEY_SCHEMA_V2``
    Current schema: ``(bag id, generation)``. New generations after a wrap get
    a disjoint key space from the bags they share an id with.

The encoded key form is the stable string that crosses the journal boundary
and is used inside materialised sets; :class:`DedupKey` is the typed form code
works with.
"""

from __future__ import annotations

from dataclasses import dataclass

KEY_SCHEMA_V1 = 1
KEY_SCHEMA_V2 = 2

# Marker journal command: payload {"from", "to", "at"}. Its position in the
# journal is the audit boundary between the two dedup key schemas.
MARKER_KIND = "dedup.schema"

# Key used for the generation 0 materialised set entry of every v1 record.
LEGACY_GENERATION = 0


@dataclass(frozen=True, order=True)
class DedupKey:
    """The composite identity dispatch deduplicates on."""

    bag_id: str
    generation: int = 0

    def encode(self) -> str:
        """Stable, collision-free token used in journals and materialised sets."""

        return encode_key(self.bag_id, self.generation)

    @classmethod
    def decode(cls, raw: str) -> "DedupKey":
        bag_id, separator, generation = raw.rpartition("#")
        if not separator:
            # A bare token is a legacy, generation-implicit v1 key.
            return cls(bag_id=raw, generation=LEGACY_GENERATION)
        return cls(bag_id=bag_id, generation=int(generation))

    def as_dict(self) -> dict[str, object]:
        return {"bag_id": self.bag_id, "generation": self.generation}


def encode_key(bag_id: str, generation: int = 0) -> str:
    return f"{bag_id}#{int(generation)}"


def decode_key(raw: str) -> DedupKey:
    return DedupKey.decode(raw)
