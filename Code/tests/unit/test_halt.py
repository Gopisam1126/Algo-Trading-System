"""The halt controller — E14-S09.

``LOW_LEVEL_ARCHITECTURE.md §8.1`` gives this module one sentence to satisfy:
*"HALTED is terminal for the day and is only exited by explicit operator
action. There is no automatic un-halt."* Most of what follows is that sentence
taken apart — terminal, explicit, operator, no automatic — and asserted a piece
at a time.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from algotrader.common.redis import keys
from algotrader.execution.halt import (
    DAILY_LOSS,
    KILL_SWITCH,
    MAX_RECORD_BYTES,
    HaltController,
    HaltReason,
    HaltRecord,
    HaltState,
    OperatorAction,
)

_ASYNC = pytest.mark.asyncio

NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)


class FakeRedis:
    """``get``, ``set``, ``delete`` — everything the controller uses.

    ``set`` records its keyword arguments so a test can assert no TTL was
    passed. That matters more than it looks: a halt with an expiry is an
    automatic un-halt on a timer, which is the one thing §8.1 forbids, and it
    would be invisible in any test that only checked the value.
    """

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.set_calls: list[tuple[str, dict]] = []
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis is unreachable")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs) -> bool:
        self._check()
        self.set_calls.append((key, kwargs))
        self.data[key] = value
        return True

    async def delete(self, key: str) -> int:
        self._check()
        return 1 if self.data.pop(key, None) is not None else 0


def _controller() -> tuple[HaltController, FakeRedis]:
    client = FakeRedis()
    return HaltController(client), client  # type: ignore[arg-type]


def _operator(name: str = "gopikrishnan") -> OperatorAction:
    return OperatorAction(operator=name, acknowledged="reviewed the loss and chose to resume")


@_ASYNC
class TestArmingWritesTheRightLatch:
    async def test_a_loss_limit_arms_its_own_latch(self) -> None:
        """Three latches, not one flag: each rejection downstream keeps its own
        reason code, which is what makes the metric answer 'why'."""
        controller, _ = _controller()
        await controller.arm(
            HaltReason.DAILY_LOSS_LIMIT, detail="down 3.2%", armed_by="risk", now=NOW
        )
        state = await controller.state()
        assert state.daily_loss is True
        assert state.kill_switch is False
        assert state.consecutive_loss is False

    async def test_consecutive_losses_arm_a_different_latch(self) -> None:
        controller, _ = _controller()
        await controller.arm(
            HaltReason.CONSECUTIVE_LOSS_LIMIT, detail="3 in a row", armed_by="risk", now=NOW
        )
        state = await controller.state()
        assert state.consecutive_loss is True
        assert state.daily_loss is False

    @pytest.mark.parametrize(
        "reason",
        [
            HaltReason.MANUAL,
            HaltReason.BROKER_DISCONNECTED,
            HaltReason.STALE_FEED,
            HaltReason.MARGIN_SHORTFALL,
            HaltReason.UNKNOWN_POSITION,
        ],
    )
    async def test_every_session_wide_reason_arms_the_kill_switch(self, reason: HaltReason) -> None:
        """The default is the kill switch, deliberately. A HaltReason added
        later that nobody mapped will halt rather than silently arm nothing."""
        controller, _ = _controller()
        await controller.arm(reason, detail="probe", armed_by="probe", now=NOW)
        assert (await controller.state()).kill_switch is True

    async def test_arming_records_why_when_and_who(self) -> None:
        controller, _ = _controller()
        await controller.arm(
            HaltReason.STALE_FEED, detail="no tick for 45s", armed_by="ingest", now=NOW
        )
        record = (await controller.state()).records[KILL_SWITCH]
        assert record.reason is HaltReason.STALE_FEED
        assert record.detail == "no tick for 45s"
        assert record.armed_by == "ingest"
        assert record.since == NOW

    async def test_the_first_reason_wins(self) -> None:
        """If the loss limit stops the day and the broker then disconnects, the
        operator needs to know a LOSS LIMIT stopped it. A later trigger
        relabelling the halt would rewrite the history of the decision."""
        controller, _ = _controller()
        first = await controller.arm(
            HaltReason.MARGIN_SHORTFALL, detail="short by 12,000", armed_by="risk", now=NOW
        )
        later = await controller.arm(
            HaltReason.BROKER_DISCONNECTED,
            detail="socket closed",
            armed_by="broker",
            now=NOW + dt.timedelta(minutes=5),
        )
        assert later == first
        assert (await controller.state()).records[KILL_SWITCH].reason is (
            HaltReason.MARGIN_SHORTFALL
        )

    async def test_a_halt_is_never_given_a_ttl(self) -> None:
        """The probe for §8.1's core claim. A TTL here is an automatic un-halt
        on a timer, and it would be invisible to any test that only checked the
        stored value."""
        controller, client = _controller()
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        assert client.set_calls
        for _key, kwargs in client.set_calls:
            assert "ex" not in kwargs
            assert "px" not in kwargs
            assert "exat" not in kwargs
            assert "keepttl" not in kwargs

    async def test_a_naive_timestamp_is_refused(self) -> None:
        """A halt is terminal for the DAY, so which day it belongs to cannot be
        ambiguous."""
        controller, _ = _controller()
        with pytest.raises(ValueError, match="naive"):
            await controller.arm(
                HaltReason.MANUAL,
                detail="d",
                armed_by="op",
                now=dt.datetime(2026, 8, 25, 6, 30),
            )

    async def test_the_kill_switch_uses_the_key_everything_else_reads(self) -> None:
        """`state.is_kill_switch_active` predates this module and reads
        `control:killswitch` by existence. Writing anywhere else would leave a
        halt that the rest of the system cannot see."""
        controller, client = _controller()
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        assert keys.kill_switch() in client.data


@_ASYNC
class TestReadingFailsClosed:
    async def test_an_unreachable_redis_reports_every_latch_armed(self) -> None:
        """The asymmetry `is_kill_switch_active` already documents: a false halt
        costs a missed trade, a false all-clear costs an uncontrolled position."""
        controller, client = _controller()
        client.fail = True
        state = await controller.state()
        assert (state.kill_switch, state.daily_loss, state.consecutive_loss) == (
            True,
            True,
            True,
        )
        assert state.any_halted is True

    async def test_an_unreadable_record_leaves_the_halt_standing(self) -> None:
        """A latch whose payload cannot be parsed is not evidence that trading
        may resume — it is evidence that something is wrong with the store
        holding the halt."""
        controller, client = _controller()
        client.data[keys.kill_switch()] = "{not json"
        state = await controller.state()
        assert state.kill_switch is True
        assert KILL_SWITCH not in state.records

    async def test_an_oversized_record_is_treated_as_unreadable(self) -> None:
        """Oversized but VALID JSON, which is the only input that isolates the
        size bound.

        The first version padded with "x", which is not JSON — so the parse
        failure rejected it and the bound was never exercised. A mutation
        removing the bound survived that test. The record here parses perfectly
        and must still be refused, because the cost being bounded is reading and
        logging a hostile value, not malformed syntax.
        """
        controller, client = _controller()
        oversized = json.dumps(
            {
                "reason": "MANUAL",
                "detail": "d" * (MAX_RECORD_BYTES * 2),
                "since": NOW.isoformat(),
                "armed_by": "op",
            }
        )
        assert len(oversized) > MAX_RECORD_BYTES
        assert json.loads(oversized), "the probe must be valid JSON or it proves nothing"

        client.data[keys.kill_switch()] = oversized
        state = await controller.state()
        assert state.kill_switch is True
        assert KILL_SWITCH not in state.records

    async def test_a_record_just_under_the_bound_is_read(self) -> None:
        """The control on the bound. A limit that rejects everything would make
        every halt unreadable, and the reason would be lost on every restart."""
        controller, client = _controller()
        client.data[keys.kill_switch()] = json.dumps(
            {
                "reason": "MANUAL",
                "detail": "d" * 100,
                "since": NOW.isoformat(),
                "armed_by": "op",
            }
        )
        assert (await controller.state()).records[KILL_SWITCH].reason is HaltReason.MANUAL

    async def test_an_unknown_reason_in_a_record_leaves_the_halt_standing(self) -> None:
        """A record naming a HaltReason this build does not have — an older or
        newer writer. Still halted."""
        controller, client = _controller()
        client.data[keys.kill_switch()] = json.dumps(
            {
                "reason": "SOMETHING_ELSE",
                "detail": "d",
                "since": NOW.isoformat(),
                "armed_by": "x",
            }
        )
        assert (await controller.state()).kill_switch is True

    async def test_a_clean_store_reports_no_halt(self) -> None:
        """The control. Everything above is satisfied by a controller that
        always reports halted, which would stop the system trading forever."""
        controller, _ = _controller()
        state = await controller.state()
        assert state.any_halted is False
        assert await controller.is_halted() is False
        assert "no halt" in state.describe()


@_ASYNC
class TestOnlyAHumanCanClear:
    async def test_a_named_operator_can_clear(self) -> None:
        controller, _ = _controller()
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        assert await controller.clear(KILL_SWITCH, _operator()) is True
        assert (await controller.state()).kill_switch is False

    async def test_clear_all_releases_every_latch(self) -> None:
        controller, _ = _controller()
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        await controller.arm(HaltReason.DAILY_LOSS_LIMIT, detail="d", armed_by="risk", now=NOW)
        released = await controller.clear_all(_operator())
        assert set(released) == {KILL_SWITCH, DAILY_LOSS}
        assert (await controller.state()).any_halted is False

    async def test_an_unnamed_operator_cannot_be_constructed(self) -> None:
        """Construction validates — one rung above a runtime check. Holding an
        OperatorAction IS the evidence that a named person acted."""
        with pytest.raises(ValueError, match="named operator"):
            OperatorAction(operator="   ", acknowledged="whatever")

    @pytest.mark.parametrize(
        "name", ["system", "SYSTEM", "auto", "Automatic", "scheduler", "execution-svc"]
    )
    async def test_the_system_cannot_clear_a_halt_as_itself(self, name: str) -> None:
        """The bypass this type exists to prevent: an automatic un-halt wearing
        a name. §8.1 — if a risk limit tripped, a HUMAN decides."""
        with pytest.raises(ValueError, match="not a person"):
            OperatorAction(operator=name, acknowledged="resuming")

    async def test_clearing_requires_acknowledging_what_is_cleared(self) -> None:
        """So the audit records a decision rather than a keystroke."""
        with pytest.raises(ValueError, match="acknowledging"):
            OperatorAction(operator="gopikrishnan", acknowledged="  ")

    async def test_an_ordinary_operator_action_is_accepted(self) -> None:
        """The control. A validator that rejects everything passes every
        hostile-input test above."""
        action = OperatorAction(operator="gopikrishnan", acknowledged="reviewed the drawdown")
        assert action.operator == "gopikrishnan"

    async def test_the_allow_list_is_not_a_constructor_parameter(self) -> None:
        """`FORBIDDEN` is a ClassVar. Annotated without it, a dataclass would
        make it a FIELD with a default — so a caller could supply their own
        empty allow-list and clear a halt as "system", defeating the type
        entirely. mypy caught this; the test keeps it caught."""
        import dataclasses

        assert [f.name for f in dataclasses.fields(OperatorAction)] == [
            "operator",
            "acknowledged",
        ]

    async def test_an_unknown_latch_name_is_refused(self) -> None:
        """A typo'd latch would delete nothing and report success, which reads
        as 'the halt was cleared'."""
        controller, _ = _controller()
        with pytest.raises(ValueError, match="not a halt latch"):
            await controller.clear("kill-switch", _operator())


class TestHaltingNeverDisablesTheExitPath:
    """E14-S09's recorded build concern, asserted structurally.

    *"A kill switch tripped at 14:00 that also stops the exit path leaves
    positions for the broker to close at 15:20 at whatever price exists."*
    The controller therefore holds a Redis client and nothing else — there is
    no object graph from here to a position, an order or the square-off timer,
    so no future edit in this file can stop the system exiting.
    """

    def test_the_controller_holds_nothing_that_could_close_a_position(self) -> None:
        controller, _ = _controller()
        held = {type(v).__name__.lower() for v in vars(controller).values()}
        for forbidden in ("position", "order", "gateway", "broker", "timer", "sizer"):
            assert not any(forbidden in name for name in held), (
                f"the halt controller holds a {forbidden!r}-like collaborator; "
                f"halting must never be able to disable the exit path"
            )

    def test_it_exposes_no_method_that_could_liquidate(self) -> None:
        """Arming a halt is not an instruction to liquidate. ExitReason.KILLSWITCH
        exists for a separate, deliberate operator command — E14-S13, not this."""
        surface = {n for n in dir(HaltController) if not n.startswith("_")}
        for forbidden in ("close", "liquidate", "exit", "square", "cancel", "flatten"):
            offenders = [n for n in surface if forbidden in n]
            assert not offenders, f"halt controller exposes {offenders}"

    def test_the_module_imports_nothing_from_the_order_or_position_path(self) -> None:
        """The strongest form: it could not reach one even indirectly."""
        import ast
        import pathlib

        import algotrader.execution.halt as halt_module

        tree = ast.parse(pathlib.Path(halt_module.__file__).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for module in imported:
            assert "repositories" not in module
            assert "broker" not in module
            assert "sizer" not in module


@_ASYNC
class TestTheRecordCannotForgeALogLine:
    """`detail` and `armed_by` are interpolated into a CRITICAL log line on
    every arm. Log forgery has been found five times in this project."""

    RAW_NEWLINE = chr(10)

    async def test_a_hostile_detail_is_escaped_at_construction(self) -> None:
        controller, _ = _controller()
        record = await controller.arm(
            HaltReason.MANUAL,
            detail="down 3%" + chr(10) + "CRITICAL kill switch disarmed by operator",
            armed_by="op",
            now=NOW,
        )
        assert self.RAW_NEWLINE not in record.detail
        assert chr(92) + "n" in record.detail

    async def test_a_hostile_armed_by_is_escaped(self) -> None:
        controller, _ = _controller()
        record = await controller.arm(
            HaltReason.MANUAL, detail="d", armed_by="risk" + chr(10) + "INFO all clear", now=NOW
        )
        assert self.RAW_NEWLINE not in record.armed_by

    async def test_a_hostile_stored_record_is_escaped_on_read(self) -> None:
        """The other door into the same field: a value written directly to
        Redis rather than through `arm`."""
        controller, client = _controller()
        client.data[keys.kill_switch()] = json.dumps(
            {
                "reason": "MANUAL",
                "detail": "x" + chr(10) + "CRITICAL disarmed",
                "since": NOW.isoformat(),
                "armed_by": "y" + chr(10) + "z",
            }
        )
        record = (await controller.state()).records[KILL_SWITCH]
        assert self.RAW_NEWLINE not in record.detail
        assert self.RAW_NEWLINE not in record.armed_by

    async def test_the_description_stays_one_line(self, caplog) -> None:
        controller, client = _controller()
        client.data[keys.kill_switch()] = json.dumps(
            {
                "reason": "MANUAL",
                "detail": "a" + chr(10) + "b",
                "since": NOW.isoformat(),
                "armed_by": "c",
            }
        )
        with caplog.at_level(logging.WARNING):
            description = (await controller.state()).describe()
        assert self.RAW_NEWLINE not in description

    async def test_an_ordinary_detail_survives_intact(self) -> None:
        """The control. Escaping must not cost the operator the message."""
        controller, _ = _controller()
        record = await controller.arm(
            HaltReason.DAILY_LOSS_LIMIT,
            detail="realised -3.2% against a 3.0% limit",
            armed_by="risk-engine",
            now=NOW,
        )
        assert record.detail == "realised -3.2% against a 3.0% limit"


@_ASYNC
class TestTheRecordRoundTrips:
    async def test_a_record_survives_json(self) -> None:
        record = HaltRecord(
            reason=HaltReason.STALE_FEED, detail="no ticks", since=NOW, armed_by="ingest"
        )
        assert HaltRecord.from_json(record.to_json()) == record

    async def test_the_stored_timestamp_is_utc(self) -> None:
        """IST only at the display boundary. A halt stored in local time would
        compare wrongly against everything else in the audit."""
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        controller, _ = _controller()
        record = await controller.arm(
            HaltReason.MANUAL,
            detail="d",
            armed_by="op",
            now=dt.datetime(2026, 8, 25, 12, 0, tzinfo=ist),
        )
        assert record.since.tzinfo is dt.UTC
        assert record.since == dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)

    async def test_a_state_with_no_records_still_describes_itself(self) -> None:
        """Fail-closed reads produce exactly this: armed, with no record to
        explain it. The description must still say something useful."""
        state = HaltState(True, False, False, {})
        assert "could not be read" in state.describe()
