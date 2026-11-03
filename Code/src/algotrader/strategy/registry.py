"""The strategy registry: persisted strategy documents and the startup load (E12-S01).

STRATEGY_ENGINE.md §9.1 replaces "strategies as a static config list" with a
registry the signal engine reads. This module is that registry's contract:
how a ``StrategyDocument`` becomes a row, and how a row becomes something the
engine may run.

Design record
-------------
**Decision.** A registered document is immutable. The registry writes it
once, in ``DRAFT``, keyed by its id and unique by its content hash, and every
load re-derives the document from the row and refuses a row that no longer
matches itself.

**Alternative rejected.** Upsert by id - "save the strategy", the way a config
row is saved. An edit would keep the validation evidence, shadow sessions and
human approval of the document it replaced: the record would say a human
approved what is running, when a human approved something else. A change is
a new version instead, a new row naming its parent (§3.1's ``orb_highvol_v3``
with ``parent_id: orb_highvol_v2``), which starts again in ``DRAFT``.

**The failure it prevents.** A strategy running that is not the strategy its
record describes: edited in place; re-registered under a new id after a
rejection, so the gauntlet sees it fresh; or altered in the table behind the
application's back. The database holds the first two structurally (no UPDATE
on the document columns, ``uq_strategy_dsl_hash``); the load holds the third.

**What would make this wrong.** If documents needed edits that keep their
state - a typo in ``name`` - every one costs a new version and a fresh trip
through validation. Names are cosmetic and nothing reads them; the cost is
deliberate.

Registration never arms anything. A row starts in ``DRAFT`` - not runnable -
and only E12-S02's state machine moves it; shipping a strategy file and
seeding it here changes nothing the signal engine evaluates.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeGuard

import yaml

from algotrader.common.enums import StrategyState
from algotrader.common.text import one_safe_line
from algotrader.strategy.dsl import (
    REGISTRY,
    CompilationError,
    PrimitiveRegistry,
    StrategyDocument,
    compile_strategy,
    load_strategy_yaml,
)
from algotrader.strategy.runtime import DEFAULT_CAPABILITIES, Capabilities, StrategyEvaluator

log = logging.getLogger(__name__)

#: A lineage longer than this is a loop someone wrote into the table, not a
#: strategy that has been revised a thousand times.
MAX_LINEAGE_DEPTH = 1000


class StrategyStore(Protocol):
    """What the registry needs from persistence - ``StrategyRepository``.

    Rows are plain dicts keyed by the ``strategy`` table's column names.
    """

    async def insert_strategy(self, row: dict[str, Any]) -> bool: ...
    async def strategy_by_id(self, strategy_id: str) -> dict[str, Any] | None: ...
    async def strategy_by_hash(self, dsl_hash: str) -> dict[str, Any] | None: ...
    async def all_strategies(self) -> list[dict[str, Any]]: ...


class RegistrationOutcome(StrEnum):
    REGISTERED = "REGISTERED"
    #: The identical document is already registered under this id. Seeding
    #: the shipped files at every start lands here, and changes nothing.
    ALREADY_REGISTERED = "ALREADY_REGISTERED"


class RegistrationRefusedError(ValueError):
    """The registry will not store this document. Nothing was written."""


class CorruptStrategyRowError(ValueError):
    """A stored row that does not describe a strategy the registry wrote."""


@dataclass(frozen=True)
class StrategyRecord:
    """One registered strategy, verified against its own row."""

    document: StrategyDocument
    state: StrategyState
    dsl_hash: str
    #: When the registry wrote the row - which is when the hypothesis was
    #: frozen (P2), and the registration order §9.3 breaks ties by.
    registered_at: dt.datetime
    state_changed_at: dt.datetime
    approved_by: str | None
    approved_at: dt.datetime | None
    retirement_reason: str | None

    @property
    def strategy_id(self) -> str:
        return self.document.id


@dataclass(frozen=True)
class RunnableStrategy:
    """A record in a state whose signals are evaluated, with its evaluator.

    Holding one is proof of three things: the row verified, the state is
    runnable, and the strategy can be answered in this deployment
    (``StrategyEvaluator`` checks capability on construction).
    """

    record: StrategyRecord
    evaluator: StrategyEvaluator

    @property
    def strategy_id(self) -> str:
        return self.record.strategy_id

    @property
    def state(self) -> StrategyState:
        return self.record.state


@dataclass(frozen=True)
class LoadedStrategies:
    """What a load found. ``rejected`` is never silently empty: every row that
    verified is in ``records``, every row that did not - and every runnable
    strategy that cannot run here - is in ``rejected`` with the reason."""

    records: Mapping[str, StrategyRecord]
    runnable: tuple[RunnableStrategy, ...]
    rejected: Mapping[str, str]


def row_for(doc: StrategyDocument, *, now: dt.datetime) -> dict[str, Any]:
    """The ``strategy`` row for a newly registered document.

    The scalar columns are copies of fields in ``dsl``, kept so the table can
    be queried; :func:`verify_row` holds them to the document on every load.
    """
    return {
        "id": doc.id,
        "name": doc.name,
        "version": doc.version,
        "parent_id": doc.parent_id,
        "origin": doc.origin.value,
        "state": StrategyState.DRAFT.value,
        "dsl": doc.model_dump(mode="json"),
        "dsl_hash": doc.content_hash(),
        "hypothesis": doc.hypothesis.model_dump(mode="json"),
        "hypothesis_frozen_at": now,
        "applicable_regimes": [r.value for r in doc.applicability.regimes],
        "created_at": doc.created_at,
        "created_by": doc.created_by,
        "approved_by": None,
        "approved_at": None,
        "state_changed_at": now,
        "retirement_reason": None,
    }


def _copied_columns(doc: StrategyDocument) -> dict[str, Any]:
    return {
        "id": doc.id,
        "name": doc.name,
        "version": doc.version,
        "parent_id": doc.parent_id,
        "origin": doc.origin.value,
        "hypothesis": doc.hypothesis.model_dump(mode="json"),
        "applicable_regimes": [r.value for r in doc.applicability.regimes],
        "created_at": doc.created_at,
        "created_by": doc.created_by,
    }


def _aware(value: object) -> TypeGuard[dt.datetime]:
    return isinstance(value, dt.datetime) and value.tzinfo is not None


def verify_row(row: Mapping[str, Any], registry: PrimitiveRegistry = REGISTRY) -> StrategyRecord:
    """Rebuild a record from its row, refusing anything the registry did not write.

    Each check closes a way for the runnable strategy to differ from the one
    the record describes:

    * the document must parse and compile - against TODAY's vocabulary, so a
      primitive removed or re-bounded since registration is caught here;
    * its content hash must be the stored ``dsl_hash`` - ``dsl`` edited behind
      the application, or a primitive's declared default changed under a
      document that omits it (see ``content_hash``);
    * every copied column must equal the document's own field - the state
      machine, the trial registry and the UI query those columns, and a row
      whose columns say one thing and whose document another is not a record
      of anything;
    * the state must be one of the ten, and a state that places live orders
      must carry its recorded approval (BR-5, read rather than trusted).

    Raises :class:`CorruptStrategyRowError`, naming the first problem.
    """
    strategy_id = row.get("id")
    try:
        doc = StrategyDocument.model_validate(row["dsl"])
        compile_strategy(doc, registry)
    except (CompilationError, ValueError, TypeError, KeyError) as exc:
        raise CorruptStrategyRowError(
            f"strategy {strategy_id!r}: the stored document does not compile: {exc}"
        ) from exc

    recomputed = doc.content_hash(registry)
    if row.get("dsl_hash") != recomputed:
        raise CorruptStrategyRowError(
            f"strategy {strategy_id!r}: the stored document hashes to {recomputed[:12]}..., "
            f"not the registered {str(row.get('dsl_hash'))[:12]}... - it is not the "
            f"document that was registered (edited in the table, or a primitive "
            f"default changed under it)"
        )
    for column, expected in _copied_columns(doc).items():
        if row.get(column) != expected:
            raise CorruptStrategyRowError(
                f"strategy {strategy_id!r}: column {column!r} disagrees with the stored "
                f"document; the row is not one the registry wrote"
            )

    try:
        state = StrategyState(row["state"])
    except (ValueError, KeyError) as exc:
        raise CorruptStrategyRowError(
            f"strategy {strategy_id!r}: state {str(row.get('state'))[:32]!r} is not a "
            f"strategy state"
        ) from exc
    approved_by, approved_at = row.get("approved_by"), row.get("approved_at")
    if state.places_live_orders and (approved_by is None or approved_at is None):
        raise CorruptStrategyRowError(
            f"strategy {strategy_id!r} is {state.value}, which places live orders, with no "
            f"recorded human approval (BR-5)"
        )

    registered_at, changed_at = row.get("hypothesis_frozen_at"), row.get("state_changed_at")
    if not (
        _aware(registered_at)
        and _aware(changed_at)
        and (approved_at is None or _aware(approved_at))
    ):
        raise CorruptStrategyRowError(
            f"strategy {strategy_id!r}: a lifecycle timestamp is missing or not timezone-aware"
        )
    return StrategyRecord(
        document=doc,
        state=state,
        dsl_hash=recomputed,
        registered_at=registered_at,
        state_changed_at=changed_at,
        approved_by=approved_by,
        approved_at=approved_at,
        retirement_reason=row.get("retirement_reason"),
    )


class StrategyRegistry:
    """Register strategy documents, and load what may run.

    Holds a :class:`StrategyStore` and nothing else. The caller owns the
    transaction (``unit_of_work`` for :meth:`register`, ``read_only`` for
    :meth:`load`), as with every other repository consumer.
    """

    def __init__(self, store: StrategyStore, registry: PrimitiveRegistry = REGISTRY) -> None:
        self._store = store
        self._registry = registry

    async def register(self, doc: StrategyDocument, *, now: dt.datetime) -> RegistrationOutcome:
        """Store ``doc`` in DRAFT, or say exactly why not.

        Refused, with nothing written: a document that does not compile; a
        different document under a registered id (registered documents are
        immutable - a change is a new version); a document that behaves
        identically to a registered strategy of ANY state, rejected and
        retired included; a version whose lineage does not hold.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        try:
            compile_strategy(doc, self._registry)
        except CompilationError as exc:
            raise RegistrationRefusedError(str(exc)) from exc
        dsl_hash = doc.content_hash(self._registry)

        existing = await self._store.strategy_by_id(doc.id)
        if existing is not None:
            return self._same_document(doc, existing)
        twin = await self._store.strategy_by_hash(dsl_hash)
        if twin is not None:
            raise self._duplicate(doc, twin)
        await self._check_lineage(doc)

        if await self._store.insert_strategy(row_for(doc, now=now)):
            log.info("registered strategy %s v%d (%s)", doc.id, doc.version, dsl_hash[:12])
            return RegistrationOutcome.REGISTERED

        # Lost a race between the reads above and the insert: the id or the
        # hash was taken in between. Answer as if the winner had come first.
        existing = await self._store.strategy_by_id(doc.id)
        if existing is not None:
            return self._same_document(doc, existing)
        twin = await self._store.strategy_by_hash(dsl_hash)
        if twin is not None:
            raise self._duplicate(doc, twin)
        raise RegistrationRefusedError(
            f"strategy {doc.id!r}: the insert was refused and no conflicting row is visible"
        )

    def _same_document(
        self, doc: StrategyDocument, existing: Mapping[str, Any]
    ) -> RegistrationOutcome:
        try:
            stored = StrategyDocument.model_validate(existing["dsl"])
        except (ValueError, TypeError, KeyError) as exc:
            raise RegistrationRefusedError(
                f"strategy id {doc.id!r} is registered, and its stored document is unreadable"
            ) from exc
        if stored == doc:
            return RegistrationOutcome.ALREADY_REGISTERED
        raise RegistrationRefusedError(
            f"strategy id {doc.id!r} is already registered with a different document. A "
            f"registered document is immutable: its validation and approval were granted "
            f"to that document. Register the change as a new id with version "
            f"{int(existing.get('version') or 0) + 1} and parent_id {doc.id!r}."
        )

    @staticmethod
    def _duplicate(doc: StrategyDocument, twin: Mapping[str, Any]) -> RegistrationRefusedError:
        return RegistrationRefusedError(
            f"strategy {doc.id!r} behaves identically to registered strategy "
            f"{twin.get('id')!r} ({twin.get('state')}). One registered strategy per "
            f"behaviour: otherwise a rejected strategy returns under a new name and is "
            f"validated as if it were new."
        )

    async def _check_lineage(self, doc: StrategyDocument) -> None:
        if doc.parent_id is None:
            if doc.version != 1:
                raise RegistrationRefusedError(
                    f"strategy {doc.id!r} is version {doc.version} with no parent. A root "
                    f"is version 1; a later version names the one it revises."
                )
            return
        if doc.parent_id == doc.id:
            raise RegistrationRefusedError(f"strategy {doc.id!r} names itself as its parent")
        parent = await self._store.strategy_by_id(doc.parent_id)
        if parent is None:
            raise RegistrationRefusedError(
                f"strategy {doc.id!r} names parent {doc.parent_id!r}, which is not registered"
            )
        expected = int(parent["version"]) + 1
        if doc.version != expected:
            raise RegistrationRefusedError(
                f"strategy {doc.id!r} is version {doc.version}, but its parent "
                f"{doc.parent_id!r} is version {parent['version']}: a revision is version "
                f"{expected}"
            )

    async def lineage(self, strategy_id: str) -> tuple[str, ...]:
        """The ids from the original version to ``strategy_id``, oldest first.

        Raises KeyError for an unregistered id, and CorruptStrategyRowError for a
        chain that loops or names a parent that is not there - neither can be
        written through :meth:`register`.
        """
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = strategy_id
        while current is not None:
            if current in seen or len(chain) >= MAX_LINEAGE_DEPTH:
                raise CorruptStrategyRowError(f"strategy {strategy_id!r}: its lineage loops")
            seen.add(current)
            row = await self._store.strategy_by_id(current)
            if row is None:
                if current == strategy_id:
                    raise KeyError(strategy_id)
                raise CorruptStrategyRowError(
                    f"strategy {strategy_id!r}: ancestor {current!r} is not registered"
                )
            chain.append(current)
            current = row.get("parent_id")
        return tuple(reversed(chain))

    async def load(self, capabilities: Capabilities = DEFAULT_CAPABILITIES) -> LoadedStrategies:
        """Every registered strategy, verified - the signal engine's startup read.

        One bad row never stops the others: it is refused, named in
        ``rejected`` and logged at ERROR, and the rest load. Only SHADOW,
        PAPER, ACTIVE and DEGRADED come back runnable
        (``StrategyState.is_runnable``), in registration order - the
        deterministic tiebreak of §9.3.
        """
        rows = await self._store.all_strategies()
        ordered = sorted(rows, key=_registration_order)

        records: dict[str, StrategyRecord] = {}
        runnable: list[RunnableStrategy] = []
        rejected: dict[str, str] = {}
        hashes: dict[str, str] = {}
        for row in ordered:
            strategy_id = str(row.get("id"))
            try:
                record = verify_row(row, self._registry)
            except CorruptStrategyRowError as exc:
                # One escaped, bounded line: the reason quotes stored values,
                # and a tampered row is exactly where those are hostile.
                rejected[strategy_id] = one_safe_line(str(exc))
                log.error("strategy refused at load: %s", rejected[strategy_id])
                continue
            twin = hashes.get(record.dsl_hash)
            if twin is not None:
                # uq_strategy_dsl_hash makes this unreachable; read anyway,
                # because a dropped constraint would otherwise run one
                # behaviour twice and double every position it takes.
                rejected[strategy_id] = (
                    f"strategy {strategy_id!r} behaves identically to {twin!r}, registered first"
                )
                log.error("strategy %s refused at load: duplicate of %s", strategy_id, twin)
                continue
            hashes[record.dsl_hash] = strategy_id
            records[strategy_id] = record
            if not record.state.is_runnable:
                continue
            try:
                evaluator = StrategyEvaluator(record.document, capabilities)
            except ValueError as exc:  # UnevaluableStrategyError, PrimitiveError
                rejected[strategy_id] = one_safe_line(
                    f"strategy {strategy_id!r} is {record.state.value} but cannot run here: {exc}"
                )
                log.error("%s", rejected[strategy_id])
                continue
            runnable.append(RunnableStrategy(record=record, evaluator=evaluator))
        return LoadedStrategies(
            records=MappingProxyType(records),
            runnable=tuple(runnable),
            rejected=MappingProxyType(rejected),
        )


def _registration_order(row: Mapping[str, Any]) -> tuple[int, str, str]:
    """Registration time, then id. A row with no readable time sorts last -
    it is about to be refused by :func:`verify_row` anyway."""
    stamp = row.get("hypothesis_frozen_at")
    if isinstance(stamp, dt.datetime) and stamp.tzinfo is not None:
        return (0, stamp.astimezone(dt.UTC).isoformat(), str(row.get("id")))
    return (1, "", str(row.get("id")))


async def register_directory(
    registry: StrategyRegistry, directory: Path, *, now: dt.datetime
) -> dict[str, str]:
    """Register every ``*.yaml`` strategy shipped in ``directory``.

    Returns ``{file name: outcome or refusal}``. A file that will not parse or
    is refused does not stop the others, and is reported - a shipped file
    edited in place under its registered id is refused here, which is the
    signal that the file and the registry have diverged. Never changes the
    state of anything already registered.
    """
    results: dict[str, str] = {}
    for path in _yaml_files(directory):
        try:
            doc = load_strategy_yaml(path.read_text(encoding="utf-8"))
            results[path.name] = (await registry.register(doc, now=now)).value
        # RegistrationRefusedError and CompilationError are ValueErrors, as is a
        # document that fails the schema; yaml.YAMLError and OSError are not.
        except (ValueError, yaml.YAMLError, OSError) as exc:
            results[path.name] = one_safe_line(f"REFUSED: {exc}")
            log.error("shipped strategy %s not registered: %s", path.name, results[path.name])
    return results


def _yaml_files(directory: Path) -> Sequence[Path]:
    return sorted(directory.glob("*.yaml")) if directory.is_dir() else ()


__all__ = [
    "MAX_LINEAGE_DEPTH",
    "CorruptStrategyRowError",
    "LoadedStrategies",
    "RegistrationOutcome",
    "RegistrationRefusedError",
    "RunnableStrategy",
    "StrategyRecord",
    "StrategyRegistry",
    "StrategyStore",
    "register_directory",
    "row_for",
    "verify_row",
]
