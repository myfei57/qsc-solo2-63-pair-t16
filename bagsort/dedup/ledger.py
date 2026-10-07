"""The committed set of dedup keys, kept consistent with the sort records.

The ledger is a projection over the journal: every post-cutover dispatch
writes a ``sort.record`` and a ``dedup.admit`` for its key, and every key
grammar boundary is declared once (``dedup.scheme``). Keeping the admissions
separate from the sort records, rather than recomputing the set each time, is
what lets :meth:`DedupLedger.reconcile_report` cross check the three things an
auditor compares by hand — the admitted key set, the sequence generation each
key carries and the sort record it authorises.

A store produced by an older build has no scheme declaration. Its first commit
under the new build seals it: prior generations keep their legacy ``v1`` keys
and the next generation uses ``v2``, so an existing snapshot replays unchanged
and wrapped bags sort afterwards. A store that starts empty is born on ``v2``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.model.entities import DedupAdmission, DedupScheme, SortRecord
from bagsort.sequence.allocator import MAX_SEQUENCE
from bagsort.store.store import Store
from .keys import SCHEME_V1, SCHEME_V2, key_for, key_v1


class _Recorder(Protocol):
    def record(self, kind: str, subject: str, stage: str, detail: dict[str, Any]) -> Any:
        ...


@dataclass(frozen=True)
class DedupDecision:
    """What the gate decided for one (bag, epoch) pair and under which key."""

    bag_id: str
    epoch: int
    key: str
    scheme: str
    allowed: bool
    existing: DedupAdmission | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "epoch": self.epoch,
            "key": self.key,
            "scheme": self.scheme,
            "allowed": self.allowed,
            "existing": self.existing.as_dict() if self.existing else None,
        }


class DedupLedger:
    """Admits dispatch keys and answers the cross-check and review questions."""

    def __init__(self, store: Store, clock: Clock, recorder: _Recorder | None = None) -> None:
        self._store = store
        self._clock = clock
        self._recorder = recorder

    def attach_recorder(self, recorder: _Recorder) -> None:
        """Provide the audit chain once it has been assembled."""

        self._recorder = recorder

    # -- scheme history ----------------------------------------------------

    def is_declared(self) -> bool:
        return bool(self._store.state().dedup_schemes)

    def history(self) -> list[DedupScheme]:
        return list(self._store.state().dedup_schemes)

    def current(self) -> DedupScheme:
        """The effective scheme, defaulting to the legacy grammar pre-declaration."""

        schemes = self._store.state().dedup_schemes
        if schemes:
            return schemes[-1]
        return DedupScheme(scheme=SCHEME_V1, cutoff_epoch=0, at="")

    def declare_scheme(
        self,
        scheme: str,
        cutoff_epoch: int,
        reason: str = "",
        audit_change: bool = True,
    ) -> DedupScheme:
        """Journal a key grammar declaration and, for a real change, audit it.

        Re-declaring the same grammar at the same boundary is a no-op, so a
        restart that replays a wrap cannot write the cutover twice. The journal
        always records the declaration (the key space must reconstruct on
        replay); the tamper-evident audit chain only records an actual grammar
        transition, which callers suppress for a brand-new store that is born on
        ``v2`` without ever having judged under ``v1``.
        """

        if scheme not in (SCHEME_V1, SCHEME_V2):
            raise ValueError(f"unknown dedup scheme {scheme!r}")
        current = self._store.state().dedup_schemes[-1:]
        if current and current[0].scheme == scheme and current[0].cutoff_epoch == cutoff_epoch:
            return current[0]
        at = self._clock.now_iso()
        declared = DedupScheme(scheme=scheme, cutoff_epoch=cutoff_epoch, at=at, reason=reason)
        with self._store.session() as session:
            session.state.dedup_schemes.append(declared)
            session.record("dedup.scheme", {"scheme": declared.as_dict()}, at)
        if audit_change and self._recorder is not None:
            self._recorder.record(
                "dedup_scheme",
                f"dedup:{scheme}",
                "audit",
                {"scheme": scheme, "cutoff_epoch": cutoff_epoch, "reason": reason},
            )
        return declared

    def declare_on_wrap(self, new_epoch: int) -> DedupScheme:
        """Handle a sequence rollover, sealing the legacy grammar when needed.

        Once the grammar is already ``v2`` a wrap changes no key grammar: the
        generation is part of every key, so consecutive generations stay
        distinct without moving the boundary. The rollover itself is still
        audit-linked (``epoch_wrap``) so the point where number spaces meet is
        traceable; a wrap that also performs the ``v1``→``v2`` transition is
        audit-linked as a key grammar change instead.
        """

        current_scheme = self.current()
        if current_scheme.scheme == SCHEME_V2 and new_epoch >= current_scheme.cutoff_epoch:
            if self._recorder is not None:
                self._recorder.record(
                    "epoch_wrap",
                    f"epoch{new_epoch}",
                    "audit",
                    {"new_epoch": new_epoch, "scheme": SCHEME_V2},
                )
            return current_scheme
        return self.declare_scheme(
            SCHEME_V2,
            new_epoch,
            reason=f"sequence wrapped into epoch {new_epoch}; key by epoch plus bag number",
        )

    def note_commit(self, epoch: int) -> DedupScheme:
        """Establish the grammar at commit time, before the bag is stored.

        An empty store adopts ``v2`` at the very first generation; a store with
        pre-upgrade bags seals its highest legacy generation so the next commit
        uses the widened key. Doing this at commit (not at first dispatch) means
        the boundary is already known by the time any bag in the new generation
        is judged, and an empty store never has to guess retroactively.
        """

        if self.is_declared():
            return self.current()
        legacy_epochs = {bag.epoch for bag in self._store.state().bag_generations}
        if not legacy_epochs:
            # Born on the widened grammar: nothing was ever judged under v1, so
            # this is a starting point, not a change, and is not audit-linked.
            return self.declare_scheme(
                SCHEME_V2, 0, reason="adopt epoch-scoped dedup keys", audit_change=False
            )
        cutoff_epoch = max(legacy_epochs) + 1
        return self.declare_scheme(
            SCHEME_V2,
            cutoff_epoch,
            reason="seal legacy bag-number keys; key by epoch plus bag number",
        )

    def ensure_declared(self) -> DedupScheme:
        """Effective scheme, declaring ``v2`` at generation 0 on a brand new store.

        Stores that cross a wrap are declared at the boundary by
        :meth:`declare_on_wrap`. Anything else reaches its first judgment
        undeclared; an empty store adopts ``v2`` immediately (its epoch-0 bag is
        already a widened key), while a store carrying pre-upgrade bags seals the
        highest legacy generation and keys the next one with ``v2``.
        """

        schemes = self._store.state().dedup_schemes
        if schemes:
            return schemes[-1]
        legacy_epochs = {bag.epoch for bag in self._store.state().bag_generations}
        if not legacy_epochs:
            cutoff_epoch = 0
            reason = "adopt epoch-scoped dedup keys"
        else:
            cutoff_epoch = max(legacy_epochs) + 1
            reason = "seal legacy bag-number keys; key by epoch plus bag number"
        return self.declare_scheme(SCHEME_V2, cutoff_epoch, reason=reason)

    # -- the gate ----------------------------------------------------------

    def _key_under_current(self, bag_id: str, epoch: int) -> tuple[str, str]:
        scheme_doc = self.current()
        if scheme_doc.scheme == SCHEME_V2 and epoch < scheme_doc.cutoff_epoch:
            # A record in the sealed history is judged with the legacy grammar.
            return key_v1(bag_id), SCHEME_V1
        return key_for(scheme_doc.scheme, bag_id, epoch), scheme_doc.scheme

    def decision_for(self, bag_id: str, epoch: int) -> DedupDecision:
        """Judge without admitting: the rule a retry or dispatch is checked by."""

        self.ensure_declared()
        key, scheme = self._key_under_current(bag_id, epoch)
        existing = next(
            (entry for entry in self._store.state().dedup_admissions if entry.key == key),
            None,
        )
        return DedupDecision(
            bag_id=bag_id,
            epoch=epoch,
            key=key,
            scheme=scheme,
            allowed=existing is None,
            existing=existing,
        )

    def is_allowed(self, bag_id: str, epoch: int) -> bool:
        return self.decision_for(bag_id, epoch).allowed

    def record_admission(self, session, bag_id: str, epoch: int, at: str) -> DedupAdmission:
        """Append the admitted key; call inside the dispatch transaction."""

        decision = self.decision_for(bag_id, epoch)
        if decision.existing is not None:
            raise ConflictError(
                f"bag {bag_id!r} in epoch {epoch} has already been sorted "
                f"under dedup key {decision.key!r}"
            )
        admission = DedupAdmission(
            scheme=decision.scheme,
            key=decision.key,
            bag_id=bag_id,
            epoch=epoch,
            at=at,
        )
        session.state.dedup_admissions.append(admission)
        session.record("dedup.admit", {"admission": admission.as_dict()}, at)
        return admission

    def admitted_keys(self) -> list[str]:
        return [entry.key for entry in self._store.state().dedup_admissions]

    # -- cross checking ----------------------------------------------------

    def reconcile_report(self) -> dict[str, Any]:
        """Cross check the admitted key set against the sort records.

        Each post-cutover dispatch writes one ``sort.record`` immediately
        followed by one ``dedup.admit``, so the widened-era admissions and
        records line up one for one. Sort records older than every scheme
        declaration predate admission tracking and are reported separately as
        ``legacy_records`` rather than counted as mismatches, which keeps old
        data queryable without pretending it always had an admission.
        """

        state = self._store.state()
        admissions = state.dedup_admissions
        records = state.sort_records
        schemes = state.dedup_schemes
        cutoff = schemes[0].cutoff_epoch if schemes else None
        widened_records = (
            [record for record in records if cutoff is not None and record.epoch >= cutoff]
            if schemes
            else []
        )
        legacy_records = len(records) - len(widened_records)
        mismatches: list[str] = []
        if len(admissions) != len(widened_records):
            mismatches.append(
                f"admission count {len(admissions)} != widened-era sort record "
                f"count {len(widened_records)} ({legacy_records} legacy record(s) excluded)"
            )
        admitted_pairs = {(admission.bag_id, admission.epoch) for admission in admissions}
        record_pairs = {(record.bag_id, record.epoch) for record in widened_records}
        for pair in sorted(admitted_pairs - record_pairs):
            mismatches.append(
                f"admitted key for {pair[0]!r} epoch {pair[1]} authorises no sort record"
            )
        for pair in sorted(record_pairs - admitted_pairs):
            mismatches.append(
                f"sort record for {pair[0]!r} epoch {pair[1]} has no admitted key"
            )
        seen: dict[str, str] = {}
        for admission in admissions:
            if admission.key in seen:
                mismatches.append(
                    f"key {admission.key!r} admitted twice ({seen[admission.key]}, {admission.at})"
                )
            seen[admission.key] = admission.at
            expected = key_for(admission.scheme, admission.bag_id, admission.epoch)
            if admission.key != expected:
                mismatches.append(
                    f"admission {admission.key!r} does not render from its scheme/epoch/number"
                )
        return {
            "ok": not mismatches,
            "admissions": len(admissions),
            "sort_records": len(records),
            "widened_records": len(widened_records),
            "legacy_records": legacy_records,
            "schemes": len(schemes),
            "mismatches": mismatches,
        }

    # -- rollover review ---------------------------------------------------

    def wrap_window(self, previous_epoch: int, limit: int = 10) -> list[dict[str, Any]]:
        """The last bags of one generation and the first bags of the next.

        These are the rows an operator re-checks one by one around a wrap. Each
        row carries the key it was admitted under, so an old bag and the wrapped
        bag that reused its number are visibly different keys.
        """

        def admission_for(record: SortRecord) -> DedupAdmission | None:
            return next(
                (
                    entry
                    for entry in self._store.state().dedup_admissions
                    if entry.bag_id == record.bag_id and entry.epoch == record.epoch
                ),
                None,
            )

        rows: list[dict[str, Any]] = []
        for record in self._store.state().sort_records:
            tail = record.epoch == previous_epoch and record.sequence > MAX_SEQUENCE - limit
            head = record.epoch == previous_epoch + 1 and record.sequence <= limit
            if not tail and not head:
                continue
            admission = admission_for(record)
            rows.append(
                {
                    "bag_id": record.bag_id,
                    "epoch": record.epoch,
                    "sequence": record.sequence,
                    "chute_id": record.chute_id,
                    "at": record.at,
                    "scheme": admission.scheme if admission else SCHEME_V1,
                    "key": admission.key if admission else key_for(SCHEME_V1, record.bag_id, record.epoch),
                }
            )
        return sorted(rows, key=lambda row: (row["epoch"], row["sequence"], row["bag_id"]))

    def wrap_pairs(self, previous_epoch: int) -> list[dict[str, Any]]:
        """Baggage numbers sorted in both generations around one wrap point."""

        rows = self.wrap_window(previous_epoch, limit=MAX_SEQUENCE)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["bag_id"], []).append(row)
        pairs: list[dict[str, Any]] = []
        for bag_id, occurrences in sorted(grouped.items()):
            epochs = {row["epoch"] for row in occurrences}
            if previous_epoch in epochs and previous_epoch + 1 in epochs:
                pairs.append(
                    {
                        "bag_id": bag_id,
                        "previous": [row for row in occurrences if row["epoch"] == previous_epoch],
                        "wrapped": [row for row in occurrences if row["epoch"] == previous_epoch + 1],
                        "distinct_keys": len({row["key"] for row in occurrences}) == 2,
                    }
                )
        return pairs
