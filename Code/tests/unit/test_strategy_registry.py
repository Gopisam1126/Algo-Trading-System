"""E12-S01 - the strategy registry, against an in-memory store.

The store here keeps the two guarantees the real table makes on insert (one
row per id, one per content hash, ``ON CONFLICT DO NOTHING``) and nothing
else, so a test can also write the rows the application never would - the
tampered, the mis-stated and the duplicated - and ask what a load makes of
them. ``tests/integration/test_strategy_registry_db.py`` asks the same of
PostgreSQL, under the application's own role.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.common.enums import StrategyState, Timeframe
from algotrader.strategy.dsl import (
    REGISTRY,
    ParamSpec,
    PrimitiveRegistry,
    StrategyDocument,
    load_strategy_yaml,
)
from algotrader.strategy.registry import (
    CorruptStrategyRowError,
    RegistrationOutcome,
    RegistrationRefusedError,
    StrategyRegistry,
    register_directory,
    row_for,
    verify_row,
)
from algotrader.strategy.runtime import Capabilities

CONFIG = Path(__file__).resolve().parents[2] / "config" / "strategies"
T0 = dt.datetime(2026, 11, 3, 3, 0, tzinfo=dt.UTC)

#: Evaluable in the default deployment (5m/15m/1h), unlike the shipped
#: orb_classic - which asks for 1d and 1w agreement; see TestLoad.
BASE_YAML = """
id: orb_test_v1
name: Opening Range Breakout - test
version: 1
origin: USER_AUTHORED
created_at: 2026-11-01T04:00:00+00:00
created_by: user
hypothesis:
  mechanism: >-
    Overnight order imbalance resolves in the first fifteen minutes; a break of
    that range on volume marks the point institutional flow commits to a
    direction for the session.
  why_it_should_persist: >-
    It is a structural consequence of how overnight orders batch into the open,
    not a pattern that arbitrages away as participants learn it.
  expected_failure_mode: >-
    Rangebound low-volatility days produce false breaks that revert immediately.
applicability: {regimes: [TRENDING, RISK_ON], timeframe: 5m, min_price: 100}
direction: LONG
entry:
  all_of:
    - {primitive: price_breaks_level, params: {level: opening_range_high, direction: above}}
    - {primitive: volume_ratio_above, params: {threshold: 1.5}}
    - {primitive: timeframe_agreement_at_least, params: {count: 2, of: "5m,15m,1h"}}
  none_of:
    - {primitive: no_material_news, params: {lookback_hours: 12}}
exit:
  stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}
  time: {primitive: squareoff_deadline}
  target: {primitive: r_multiple_target, params: {r: 2.0}}
constraints: {max_entries_per_day: 1, entry_window: ["09:30", "11:00"]}
"""


def _base() -> StrategyDocument:
    return load_strategy_yaml(BASE_YAML)


def _variant(edit: Callable[[dict[str, Any]], None], base: StrategyDocument | None = None):
    data = copy.deepcopy((base or _base()).model_dump(mode="json"))
    edit(data)
    return StrategyDocument.model_validate(data)


def _renamed(new_id: str, **fields: Any) -> Callable[[dict[str, Any]], None]:
    def edit(d: dict[str, Any]) -> None:
        d["id"] = new_id
        d.update(fields)

    return edit


def _retuned(threshold: float) -> Callable[[dict[str, Any]], None]:
    """A genuinely different strategy: the volume threshold moves."""

    def edit(d: dict[str, Any]) -> None:
        d["entry"]["all_of"][1]["params"]["threshold"] = threshold

    return edit


class MemoryStore:
    """The ``strategy`` table's insert guarantees, and nothing more."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        #: Run once just before the next insert - a writer racing this one.
        self.before_insert: Callable[[], None] | None = None

    async def insert_strategy(self, row: dict[str, Any]) -> bool:
        self.calls.append("insert_strategy")
        if self.before_insert is not None:
            racer, self.before_insert = self.before_insert, None
            racer()
        if row["id"] in self.rows or any(
            r["dsl_hash"] == row["dsl_hash"] for r in self.rows.values()
        ):
            return False
        self.rows[row["id"]] = copy.deepcopy(row)
        return True

    async def strategy_by_id(self, strategy_id: str) -> dict[str, Any] | None:
        self.calls.append("strategy_by_id")
        row = self.rows.get(strategy_id)
        return copy.deepcopy(row) if row is not None else None

    async def strategy_by_hash(self, dsl_hash: str) -> dict[str, Any] | None:
        self.calls.append("strategy_by_hash")
        for row in self.rows.values():
            if row["dsl_hash"] == dsl_hash:
                return copy.deepcopy(row)
        return None

    async def all_strategies(self) -> list[dict[str, Any]]:
        self.calls.append("all_strategies")
        return [copy.deepcopy(r) for r in self.rows.values()]

    # -- what the application cannot do, for the tests that need it done -----

    def set_state(self, strategy_id: str, state: StrategyState, *, approved: bool = False) -> None:
        row = self.rows[strategy_id]
        row["state"] = state.value
        if approved:
            row["approved_by"], row["approved_at"] = "operator", T0 + dt.timedelta(days=1)

    def force(self, row: dict[str, Any]) -> None:
        """Write a row past every guarantee, as a hand-run SQL statement would."""
        self.rows[row["id"]] = copy.deepcopy(row)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def registry(store: MemoryStore) -> StrategyRegistry:
    return StrategyRegistry(store)


# ---------------------------------------------------------------------------
# AC4 - the content hash is a hash of behaviour
# ---------------------------------------------------------------------------


class TestTheContentHashIsAHashOfBehaviour:
    """Two documents that behave identically hash identically; any behavioural
    difference hashes apart. The registry's duplicate refusal is only as good
    as the first half, and the trial count only as honest as the second."""

    def test_numbers_hash_by_value(self) -> None:
        base = _base()

        def respell(d: dict[str, Any]) -> None:
            d["exit"]["target"]["params"]["r"] = 2  # 2.0 as written
            d["entry"]["all_of"][1]["params"]["threshold"] = 1.50
            d["applicability"]["min_price"] = "100.000"

        assert _variant(respell).content_hash() == base.content_hash()

    def test_a_stated_default_is_the_default(self) -> None:
        """``period: 14`` on atr_stop is its declared default, and ``of`` on
        timeframe_agreement_at_least is ``5m,15m,1h`` by default."""

        def omit(d: dict[str, Any]) -> None:
            del d["exit"]["stop"]["params"]["period"]
            del d["entry"]["all_of"][2]["params"]["of"]

        assert _variant(omit).content_hash() == _base().content_hash()

    def test_order_and_repetition_within_a_group_change_nothing(self) -> None:
        def shuffle(d: dict[str, Any]) -> None:
            group = d["entry"]["all_of"]
            d["entry"]["all_of"] = [group[2], group[0], group[1], group[0]]

        assert _variant(shuffle).content_hash() == _base().content_hash()

    def test_regime_and_sector_order_change_nothing(self) -> None:
        with_sectors = _variant(lambda d: d["applicability"].update(sectors=["IT", "BANK"]))
        reordered = _variant(
            lambda d: d["applicability"].update(
                regimes=["RISK_ON", "TRENDING", "RISK_ON"], sectors=["BANK", "IT", "BANK"]
            )
        )
        assert reordered.content_hash() == with_sectors.content_hash()

    def test_metadata_changes_nothing(self) -> None:
        def relabel(d: dict[str, Any]) -> None:
            d.update(
                id="something_else",
                name="Another name entirely",
                version=4,
                parent_id="orb_test_v3",
                origin="AI_PROPOSED_JOURNAL",
                created_at="2027-01-01T00:00:00+00:00",
                created_by="claude-opus-5-5",
            )
            d["hypothesis"]["mechanism"] += " Restated at greater length, saying nothing new."

        assert _variant(relabel).content_hash() == _base().content_hash()

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda d: d.update(direction="SHORT"), id="direction"),
            pytest.param(_retuned(1.6), id="a-threshold"),
            pytest.param(lambda d: d["applicability"].update(timeframe="15m"), id="timeframe"),
            pytest.param(lambda d: d["applicability"].update(min_price="150"), id="min-price"),
            pytest.param(lambda d: d["applicability"].update(regimes=["TRENDING"]), id="regimes"),
            pytest.param(lambda d: d["applicability"].update(sectors=["IT"]), id="sectors"),
            pytest.param(
                lambda d: d["entry"]["none_of"].append(d["entry"]["all_of"].pop(1)),
                id="condition-moved-between-groups",
            ),
            pytest.param(lambda d: d["entry"]["all_of"].pop(1), id="condition-removed"),
            pytest.param(lambda d: d["exit"]["stop"]["params"].update(period=10), id="stop-period"),
            pytest.param(lambda d: d["exit"]["target"]["params"].update(r=3.0), id="target"),
            pytest.param(lambda d: d["exit"].update(target=None), id="target-removed"),
            pytest.param(
                lambda d: d["constraints"].update(max_entries_per_day=2), id="max-entries"
            ),
            pytest.param(
                lambda d: d["constraints"].update(entry_window=["09:30", "11:05"]),
                id="entry-window",
            ),
        ],
    )
    def test_every_behavioural_difference_hashes_apart(
        self, edit: Callable[[dict[str, Any]], None]
    ) -> None:
        """The control. A hash canonical over too much would merge two
        strategies, and the trial count would understate the search."""
        assert _variant(edit).content_hash() != _base().content_hash()

    def test_a_changed_default_changes_the_hash_of_a_document_that_omits_it(self) -> None:
        """The stored hash then stops matching, and the load refuses the row -
        correctly, because that strategy no longer behaves as validated."""
        omitting = _variant(lambda d: d["exit"]["stop"]["params"].pop("period"))
        stating = _base()

        retuned = PrimitiveRegistry()
        for name in REGISTRY.names():
            spec = REGISTRY.get(name)
            if name == "atr_stop":
                params = [
                    ParamSpec(**{**p.model_dump(), "default": 10}) if p.name == "period" else p
                    for p in spec.params
                ]
                spec = spec.model_copy(update={"params": params})
            retuned.register(spec)

        assert omitting.content_hash(retuned) != omitting.content_hash()
        assert stating.content_hash(retuned) == stating.content_hash()

    @settings(max_examples=60, deadline=None)
    @given(order=st.permutations(range(3)), regimes=st.permutations(["TRENDING", "RISK_ON"]))
    def test_any_permutation_hashes_the_same(self, order: list[int], regimes: list[str]) -> None:
        def permute(d: dict[str, Any]) -> None:
            d["entry"]["all_of"] = [d["entry"]["all_of"][i] for i in order]
            d["applicability"]["regimes"] = regimes

        assert _variant(permute).content_hash() == _base().content_hash()


# ---------------------------------------------------------------------------
# AC1 - registration
# ---------------------------------------------------------------------------


class TestRegistration:
    async def test_a_document_is_stored_in_draft_with_its_hash_and_a_frozen_hypothesis(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        doc = _base()
        assert await registry.register(doc, now=T0) is RegistrationOutcome.REGISTERED

        row = store.rows[doc.id]
        assert row["state"] == "DRAFT"
        assert row["approved_by"] is None and row["approved_at"] is None
        assert row["dsl_hash"] == doc.content_hash()
        assert row["hypothesis_frozen_at"] == T0 and row["state_changed_at"] == T0
        assert StrategyDocument.model_validate(row["dsl"]) == doc

    async def test_the_same_document_again_changes_nothing(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        doc = _base()
        await registry.register(doc, now=T0)
        later = T0 + dt.timedelta(days=3)
        assert await registry.register(doc, now=later) is RegistrationOutcome.ALREADY_REGISTERED
        assert len(store.rows) == 1
        assert store.rows[doc.id]["hypothesis_frozen_at"] == T0, "the hypothesis was re-frozen"

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(_retuned(1.8), id="behaviour"),
            pytest.param(lambda d: d.update(name="Renamed but identical"), id="name-only"),
            pytest.param(
                lambda d: d["hypothesis"].update(
                    expected_failure_mode="Fails whenever the breakout is a news spike."
                ),
                id="hypothesis-only",
            ),
        ],
    )
    async def test_a_different_document_under_a_registered_id_is_refused(
        self,
        registry: StrategyRegistry,
        store: MemoryStore,
        edit: Callable[[dict[str, Any]], None],
    ) -> None:
        """Immutable means every field: a hypothesis rewritten after the fact
        is precisely what freezing it exists to prevent."""
        doc = _base()
        await registry.register(doc, now=T0)
        before = copy.deepcopy(store.rows)
        with pytest.raises(RegistrationRefusedError, match=r"immutable.*version 2.*parent_id"):
            await registry.register(_variant(edit), now=T0)
        assert store.rows == before

    async def test_a_document_that_does_not_compile_is_never_written(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        """A target in the stop slot parses, and is a strategy with no stop."""
        no_stop = _variant(
            lambda d: d["exit"].update(
                stop={"primitive": "r_multiple_target", "params": {"r": 2.0}}
            )
        )
        with pytest.raises(RegistrationRefusedError, match=r"exit.stop"):
            await registry.register(no_stop, now=T0)
        assert store.rows == {}
        assert "insert_strategy" not in store.calls

    async def test_a_naive_clock_is_refused(self, registry: StrategyRegistry) -> None:
        with pytest.raises(ValueError, match="timezone"):
            await registry.register(_base(), now=dt.datetime(2026, 11, 3, 9, 0))

    async def test_a_lost_race_for_the_id_is_answered_as_if_the_winner_came_first(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        doc = _base()
        store.before_insert = lambda: store.force(row_for(doc, now=T0))
        assert await registry.register(doc, now=T0) is RegistrationOutcome.ALREADY_REGISTERED

        other = _variant(_retuned(1.9), _variant(_renamed("orb_race_v1")))
        store.before_insert = lambda: store.force(row_for(_variant(_retuned(2.5), other), now=T0))
        with pytest.raises(RegistrationRefusedError, match="immutable"):
            await registry.register(other, now=T0)

    async def test_a_lost_race_for_the_behaviour_names_the_winner(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        doc = _base()
        twin = _variant(_renamed("orb_twin_v1"))
        store.before_insert = lambda: store.force(row_for(twin, now=T0))
        with pytest.raises(RegistrationRefusedError, match="orb_twin_v1"):
            await registry.register(doc, now=T0)


# ---------------------------------------------------------------------------
# AC4 - one registered strategy per behaviour
# ---------------------------------------------------------------------------


class TestOneStrategyPerBehaviour:
    async def test_a_twin_under_a_new_id_is_refused_naming_the_original(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        await registry.register(_base(), now=T0)
        with pytest.raises(RegistrationRefusedError, match=r"identically.*'orb_test_v1'.*DRAFT"):
            await registry.register(_variant(_renamed("orb_copy_v1")), now=T0)
        assert set(store.rows) == {"orb_test_v1"}

    @pytest.mark.parametrize("state", [StrategyState.REJECTED, StrategyState.RETIRED])
    async def test_a_rejected_or_retired_strategy_cannot_return_respelled(
        self, registry: StrategyRegistry, store: MemoryStore, state: StrategyState
    ) -> None:
        """The failure dedup exists for: search-until-something-passes, one
        cosmetic respelling at a time."""
        await registry.register(_base(), now=T0)
        store.set_state("orb_test_v1", state)

        def respell(d: dict[str, Any]) -> None:
            d.update(id="orb_fresh_idea_v1", name="A fresh idea", origin="AI_PROPOSED_JOURNAL")
            d["exit"]["target"]["params"]["r"] = 2
            d["entry"]["all_of"].reverse()
            del d["exit"]["stop"]["params"]["period"]

        with pytest.raises(RegistrationRefusedError, match=state.value):
            await registry.register(_variant(respell), now=T0)

    async def test_a_twin_is_refused_before_any_insert_even_by_a_table_without_the_guarantee(
        self,
    ) -> None:
        """Mutation 'a twin is not refused' survived the first run (3 Nov 2026):
        the store's insert refused the duplicate hash and the lost-race path
        named the twin, so removing the registry's own check changed nothing
        any test saw. The check is the registration-time guard if the table
        ever lacks uq_strategy_dsl_hash; this store does not keep it."""

        class NoHashGuarantee(MemoryStore):
            async def insert_strategy(self, row: dict[str, Any]) -> bool:
                self.calls.append("insert_strategy")
                if row["id"] in self.rows:
                    return False
                self.rows[row["id"]] = copy.deepcopy(row)
                return True

        store = NoHashGuarantee()
        registry = StrategyRegistry(store)
        await registry.register(_base(), now=T0)
        store.calls.clear()
        with pytest.raises(RegistrationRefusedError, match="'orb_test_v1'"):
            await registry.register(_variant(_renamed("orb_copy_v1")), now=T0)
        assert set(store.rows) == {"orb_test_v1"}
        assert "insert_strategy" not in store.calls

    async def test_a_genuine_variant_registers(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        """The control: dedup must not refuse a strategy that differs."""
        await registry.register(_base(), now=T0)
        variant = _variant(_retuned(1.8), _variant(_renamed("orb_vol18_v1")))
        assert await registry.register(variant, now=T0) is RegistrationOutcome.REGISTERED


# ---------------------------------------------------------------------------
# AC3 - versions and lineage
# ---------------------------------------------------------------------------


def _child(parent: StrategyDocument, new_id: str, version: int, threshold: float):
    return _variant(
        _retuned(threshold),
        _variant(_renamed(new_id, version=version, parent_id=parent.id), parent),
    )


class TestLineage:
    async def test_a_revision_names_its_parent_and_is_the_next_version(
        self, registry: StrategyRegistry
    ) -> None:
        v1 = _base()
        v2 = _child(v1, "orb_test_v2", 2, 1.8)
        v3 = _child(v2, "orb_test_v3", 3, 2.1)
        for doc in (v1, v2, v3):
            assert await registry.register(doc, now=T0) is RegistrationOutcome.REGISTERED
        assert await registry.lineage("orb_test_v3") == (
            "orb_test_v1",
            "orb_test_v2",
            "orb_test_v3",
        )
        assert await registry.lineage("orb_test_v1") == ("orb_test_v1",)

    async def test_siblings_may_revise_the_same_parent(self, registry: StrategyRegistry) -> None:
        v1 = _base()
        await registry.register(v1, now=T0)
        await registry.register(_child(v1, "orb_test_v2a", 2, 1.8), now=T0)
        assert (
            await registry.register(_child(v1, "orb_test_v2b", 2, 2.2), now=T0)
            is RegistrationOutcome.REGISTERED
        )

    @pytest.mark.parametrize(
        ("doc_fn", "message"),
        [
            pytest.param(
                lambda v1: _variant(_retuned(1.8), _variant(_renamed("orb_orphan_v3", version=3))),
                "no parent",
                id="root-not-v1",
            ),
            pytest.param(
                lambda v1: _variant(
                    _retuned(1.8),
                    _variant(_renamed("orb_test_v2", version=2, parent_id="nobody_v1")),
                ),
                "not registered",
                id="parent-missing",
            ),
            pytest.param(
                lambda v1: _child(v1, "orb_test_v3", 3, 1.8), "version 2", id="skips-a-version"
            ),
            pytest.param(
                lambda v1: _child(v1, "orb_test_v1b", 1, 1.8), "version 2", id="same-version"
            ),
            pytest.param(
                lambda v1: _variant(
                    _retuned(1.8),
                    _variant(_renamed("orb_self_v2", version=2, parent_id="orb_self_v2")),
                ),
                "itself",
                id="own-parent",
            ),
        ],
    )
    async def test_a_broken_lineage_is_refused(
        self,
        registry: StrategyRegistry,
        store: MemoryStore,
        doc_fn: Callable[[StrategyDocument], StrategyDocument],
        message: str,
    ) -> None:
        v1 = _base()
        await registry.register(v1, now=T0)
        with pytest.raises(RegistrationRefusedError, match=message):
            await registry.register(doc_fn(v1), now=T0)
        assert set(store.rows) == {"orb_test_v1"}

    async def test_an_unregistered_id_has_no_lineage(self, registry: StrategyRegistry) -> None:
        with pytest.raises(KeyError):
            await registry.lineage("never_registered")

    async def test_a_loop_written_into_the_table_is_reported_not_followed(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        v1 = _base()
        v2 = _child(v1, "orb_test_v2", 2, 1.8)
        await registry.register(v1, now=T0)
        await registry.register(v2, now=T0)
        store.rows["orb_test_v1"]["parent_id"] = "orb_test_v2"
        with pytest.raises(CorruptStrategyRowError, match="loops"):
            await registry.lineage("orb_test_v2")

    async def test_a_missing_ancestor_is_reported(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        v1 = _base()
        await registry.register(v1, now=T0)
        await registry.register(_child(v1, "orb_test_v2", 2, 1.8), now=T0)
        del store.rows["orb_test_v1"]
        with pytest.raises(CorruptStrategyRowError, match="ancestor 'orb_test_v1'"):
            await registry.lineage("orb_test_v2")


# ---------------------------------------------------------------------------
# AC2 - the startup load
# ---------------------------------------------------------------------------


async def _registered(registry: StrategyRegistry, *docs: StrategyDocument) -> None:
    for i, doc in enumerate(docs):
        await registry.register(doc, now=T0 + dt.timedelta(minutes=i))


def _three() -> tuple[StrategyDocument, StrategyDocument, StrategyDocument]:
    return (
        _base(),
        _variant(_retuned(1.8), _variant(_renamed("orb_b_v1"))),
        _variant(_retuned(2.2), _variant(_renamed("orb_c_v1"))),
    )


class TestTheStartupLoad:
    async def test_a_draft_loads_as_a_record_and_does_not_run(
        self, registry: StrategyRegistry
    ) -> None:
        await registry.register(_base(), now=T0)
        loaded = await registry.load()
        assert set(loaded.records) == {"orb_test_v1"}
        assert loaded.records["orb_test_v1"].document == _base()
        assert loaded.runnable == () and dict(loaded.rejected) == {}

    @pytest.mark.parametrize("state", list(StrategyState))
    async def test_a_strategy_runs_exactly_when_its_state_is_runnable(
        self, registry: StrategyRegistry, store: MemoryStore, state: StrategyState
    ) -> None:
        await registry.register(_base(), now=T0)
        store.set_state("orb_test_v1", state, approved=state.places_live_orders)
        loaded = await registry.load()
        assert [r.strategy_id for r in loaded.runnable] == (
            ["orb_test_v1"] if state.is_runnable else []
        )
        assert dict(loaded.rejected) == {}

    def test_the_runnable_states_are_the_four_the_design_names(self) -> None:
        """§9.1: ACTIVE places orders, PAPER and SHADOW record. Pinned, because
        a fifth runnable state would be evaluated by the signal engine."""
        assert {s for s in StrategyState if s.is_runnable} == {
            StrategyState.SHADOW,
            StrategyState.PAPER,
            StrategyState.ACTIVE,
            StrategyState.DEGRADED,
        }

    async def test_runnable_strategies_come_back_in_registration_order(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        """By registration time, whatever order the store returns rows in.

        The first version registered in time order, so the store's insertion
        order WAS registration order and mutation 'registration order ignored'
        survived (3 Nov 2026). Here the store returns b, c, a and the times
        say c, a, b."""
        a, b, c = _three()
        await registry.register(b, now=T0 + dt.timedelta(minutes=2))
        await registry.register(c, now=T0)
        await registry.register(a, now=T0 + dt.timedelta(minutes=1))
        assert list(store.rows) == ["orb_b_v1", "orb_c_v1", "orb_test_v1"]
        for sid in store.rows:
            store.set_state(sid, StrategyState.PAPER)
        loaded = await registry.load()
        assert [r.strategy_id for r in loaded.runnable] == ["orb_c_v1", "orb_test_v1", "orb_b_v1"]

    @pytest.mark.parametrize(
        ("tamper", "reason"),
        [
            pytest.param(
                lambda r: r["dsl"]["entry"]["all_of"][1]["params"].update(threshold=0.2),
                "hashes to",
                id="document-edited",
            ),
            pytest.param(lambda r: r.update(dsl_hash="f" * 64), "hashes to", id="hash-edited"),
            pytest.param(
                lambda r: r.update(applicable_regimes=["TRENDING", "RISK_ON", "HIGH_VOL"]),
                "'applicable_regimes'",
                id="regimes-column",
            ),
            pytest.param(lambda r: r.update(origin="BUILTIN_SEED"), "'origin'", id="origin-column"),
            pytest.param(lambda r: r.update(version=2), "'version'", id="version-column"),
            pytest.param(lambda r: r.update(created_by="operator"), "'created_by'", id="author"),
            pytest.param(
                lambda r: r["hypothesis"].update(mechanism="x" * 90),
                "'hypothesis'",
                id="hypothesis",
            ),
            pytest.param(
                lambda r: r.update(state="active"), "not a strategy state", id="state-case"
            ),
            pytest.param(lambda r: r.update(state="ACTIVE"), "BR-5", id="active-unapproved"),
            pytest.param(lambda r: r.update(state="DEGRADED"), "BR-5", id="degraded-unapproved"),
            pytest.param(
                lambda r: r["dsl"].update(leverage=5), "does not compile", id="extra-field"
            ),
            pytest.param(
                lambda r: r["dsl"].pop("hypothesis"), "does not compile", id="no-hypothesis"
            ),
            pytest.param(
                lambda r: r.update(dsl="not a document"), "does not compile", id="not-a-dict"
            ),
            pytest.param(
                lambda r: r.update(hypothesis_frozen_at=dt.datetime(2026, 11, 3, 9, 0)),
                "timezone-aware",
                id="naive-timestamp",
            ),
            pytest.param(
                lambda r: r.update(state_changed_at=None), "timezone-aware", id="no-timestamp"
            ),
        ],
    )
    async def test_a_row_that_is_not_what_was_registered_is_refused_and_the_rest_load(
        self,
        registry: StrategyRegistry,
        store: MemoryStore,
        tamper: Callable[[dict[str, Any]], None],
        reason: str,
    ) -> None:
        await _registered(registry, *_three())
        for sid in store.rows:
            store.set_state(sid, StrategyState.PAPER)
        tamper(store.rows["orb_test_v1"])

        loaded = await registry.load()
        assert set(loaded.rejected) == {"orb_test_v1"}
        assert reason in loaded.rejected["orb_test_v1"]
        assert "orb_test_v1" not in loaded.records
        assert [r.strategy_id for r in loaded.runnable] == ["orb_b_v1", "orb_c_v1"]

    async def test_an_approved_live_state_is_not_refused(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        """The control for the two BR-5 cases above."""
        await registry.register(_base(), now=T0)
        store.set_state("orb_test_v1", StrategyState.DEGRADED, approved=True)
        loaded = await registry.load()
        assert [r.strategy_id for r in loaded.runnable] == ["orb_test_v1"]
        assert loaded.runnable[0].state is StrategyState.DEGRADED

    async def test_a_primitive_rebounded_since_registration_is_caught_at_load(
        self, store: MemoryStore
    ) -> None:
        """Compiled against TODAY's vocabulary: a threshold of 1.5 that a
        later code change forbids does not keep running on the old rule."""
        await StrategyRegistry(store).register(_base(), now=T0)

        tightened = PrimitiveRegistry()
        for name in REGISTRY.names():
            spec = REGISTRY.get(name)
            if name == "volume_ratio_above":
                params = [
                    ParamSpec(**{**p.model_dump(), "maximum": Decimal("1.2")})
                    if p.name == "threshold"
                    else p
                    for p in spec.params
                ]
                spec = spec.model_copy(update={"params": params})
            tightened.register(spec)

        loaded = await StrategyRegistry(store, tightened).load()
        assert "above maximum" in loaded.rejected["orb_test_v1"]

    async def test_a_runnable_strategy_this_deployment_cannot_answer_is_rejected_but_recorded(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        await registry.register(_base(), now=T0)
        store.set_state("orb_test_v1", StrategyState.SHADOW)
        without_1h = Capabilities(
            indicators=Capabilities.from_engine().indicators,
            timeframes=frozenset({Timeframe.M5, Timeframe.M15}),
            historical=Capabilities.from_engine().historical,
        )
        loaded = await registry.load(without_1h)
        assert loaded.runnable == ()
        assert "cannot run here" in loaded.rejected["orb_test_v1"]
        assert "orb_test_v1" in loaded.records, "it is a sound record; it just cannot run here"

    async def test_a_duplicate_behaviour_past_the_constraint_runs_once(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        """Unreachable while uq_strategy_dsl_hash exists; read anyway."""
        await registry.register(_base(), now=T0)
        twin = row_for(_variant(_renamed("orb_twin_v1")), now=T0 + dt.timedelta(minutes=5))
        store.force(twin)
        for sid in store.rows:
            store.set_state(sid, StrategyState.PAPER)
        loaded = await registry.load()
        assert [r.strategy_id for r in loaded.runnable] == ["orb_test_v1"]
        assert "orb_test_v1" in loaded.rejected["orb_twin_v1"]

    async def test_what_a_load_returns_cannot_be_edited(self, registry: StrategyRegistry) -> None:
        await registry.register(_base(), now=T0)
        loaded = await registry.load()
        with pytest.raises(TypeError):
            loaded.records["x"] = loaded.records["orb_test_v1"]  # type: ignore[index]
        with pytest.raises(TypeError):
            loaded.rejected["x"] = "y"  # type: ignore[index]

    def test_verify_row_accepts_exactly_what_row_for_writes(self) -> None:
        """The two are each other's contract; the row the registry writes must
        be the row the registry reads."""
        doc = _base()
        record = verify_row(row_for(doc, now=T0))
        assert record.document == doc and record.state is StrategyState.DRAFT
        assert record.registered_at == T0 and record.dsl_hash == doc.content_hash()

    async def test_the_shipped_seed_is_refused_at_load_until_the_engine_carries_1d_and_1w(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        """A fact the load surfaces rather than hides (3 Nov 2026): orb_classic
        asks for 1h/1d/1w trend agreement and the indicator engine carries
        5m/15m/1h. Promoted to a runnable state, it is refused by name."""
        seed = load_strategy_yaml((CONFIG / "orb_classic.yaml").read_text(encoding="utf-8"))
        await registry.register(seed, now=T0)
        store.set_state("orb_classic", StrategyState.SHADOW)
        loaded = await registry.load()
        assert "'1d' is not carried" in loaded.rejected["orb_classic"]


# ---------------------------------------------------------------------------
# Seeding the shipped files
# ---------------------------------------------------------------------------


class TestSeedingTheShippedFiles:
    async def test_the_shipped_strategies_register_once(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        first = await register_directory(registry, CONFIG, now=T0)
        assert first == {"orb_classic.yaml": "REGISTERED"}
        again = await register_directory(registry, CONFIG, now=T0 + dt.timedelta(days=1))
        assert again == {"orb_classic.yaml": "ALREADY_REGISTERED"}
        assert store.rows["orb_classic"]["state"] == "DRAFT"

    async def test_seeding_never_changes_a_state(
        self, registry: StrategyRegistry, store: MemoryStore
    ) -> None:
        await register_directory(registry, CONFIG, now=T0)
        store.set_state("orb_classic", StrategyState.RETIRED)
        await register_directory(registry, CONFIG, now=T0 + dt.timedelta(days=1))
        assert store.rows["orb_classic"]["state"] == "RETIRED"

    async def test_a_bad_file_is_reported_and_the_others_still_register(
        self, registry: StrategyRegistry, store: MemoryStore, tmp_path: Path
    ) -> None:
        (tmp_path / "a_good.yaml").write_text(BASE_YAML, encoding="utf-8")
        (tmp_path / "b_broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        (tmp_path / "c_schema.yaml").write_text("id: x\n", encoding="utf-8")
        (tmp_path / "d_edited.yaml").write_text(
            BASE_YAML.replace("threshold: 1.5", "threshold: 1.7"), encoding="utf-8"
        )
        results = await register_directory(registry, tmp_path, now=T0)
        assert results["a_good.yaml"] == "REGISTERED"
        assert results["b_broken.yaml"].startswith("REFUSED")
        assert results["c_schema.yaml"].startswith("REFUSED")
        assert (
            results["d_edited.yaml"].startswith("REFUSED")
            and "immutable" in results["d_edited.yaml"]
        )
        assert set(store.rows) == {"orb_test_v1"}

    async def test_no_directory_registers_nothing(
        self, registry: StrategyRegistry, tmp_path: Path
    ) -> None:
        assert await register_directory(registry, tmp_path / "absent", now=T0) == {}
