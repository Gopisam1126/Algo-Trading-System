"""The halt against a real Redis — E14-S09.

Three claims cannot be made with a double, because each is about the store
rather than the code:

* **A halt survives a restart.** "Terminal for the day" is a claim about
  persistence, and a controller that only remembered in-process would satisfy
  every unit test here while forgetting the halt the moment the service
  restarted — which is exactly when a halt matters most.
* **A halt has no expiry.** The unit suite asserts no TTL *argument* was
  passed. Only Redis can say whether the key actually has one.
* **The rest of the system sees it.** ``state.is_kill_switch_active`` predates
  this module, reads the same key by existence, and is what the risk engine
  will consult. Two components, one key, and nothing but an integration test
  puts them in the same room.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from algotrader.common.redis import keys, state
from algotrader.execution.halt import (
    CONSECUTIVE_LOSS,
    DAILY_LOSS,
    KILL_SWITCH,
    HaltController,
    HaltReason,
    OperatorAction,
)

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def _operator() -> OperatorAction:
    return OperatorAction(
        operator="gopikrishnan", acknowledged="reviewed the drawdown and chose to resume"
    )


class TestAHaltSurvivesARestart:
    async def test_a_new_controller_sees_the_halt(self, r: aioredis.Redis) -> None:
        """The restart, modelled honestly: a second controller with no shared
        memory, reading the same store."""
        await HaltController(r).arm(
            HaltReason.DAILY_LOSS_LIMIT, detail="down 3.2%", armed_by="risk", now=NOW
        )
        after_restart = HaltController(r)
        assert (await after_restart.state()).daily_loss is True

    async def test_the_reason_survives_too(self, r: aioredis.Redis) -> None:
        """Not merely "something halted us". An operator restarting into a
        halted session needs to know which limit tripped before deciding
        whether resuming is appropriate — §8.1's whole premise."""
        await HaltController(r).arm(
            HaltReason.MARGIN_SHORTFALL, detail="short by 12,000", armed_by="risk", now=NOW
        )
        record = (await HaltController(r).state()).records[KILL_SWITCH]
        assert record.reason is HaltReason.MARGIN_SHORTFALL
        assert record.detail == "short by 12,000"
        assert record.since == NOW

    async def test_a_cleared_halt_stays_cleared_across_a_restart(self, r: aioredis.Redis) -> None:
        """The control. A store that never forgets is as broken as one that
        never remembers — the operator would be unable to resume."""
        controller = HaltController(r)
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        await controller.clear(KILL_SWITCH, _operator())
        assert (await HaltController(r).state()).any_halted is False


class TestAHaltHasNoExpiry:
    """§8.1 forbids an automatic un-halt. A TTL is an automatic un-halt on a
    timer, and only the store can be asked whether one exists."""

    @pytest.mark.parametrize(
        ("reason", "latch"),
        [
            (HaltReason.MANUAL, KILL_SWITCH),
            (HaltReason.DAILY_LOSS_LIMIT, DAILY_LOSS),
            (HaltReason.CONSECUTIVE_LOSS_LIMIT, CONSECUTIVE_LOSS),
        ],
    )
    async def test_redis_reports_no_expiry(
        self, r: aioredis.Redis, reason: HaltReason, latch: str
    ) -> None:
        await HaltController(r).arm(reason, detail="d", armed_by="op", now=NOW)
        key = keys.kill_switch() if latch == KILL_SWITCH else keys.halt_latch(latch)
        # -1 means "exists, no expiry". -2 would mean the key is absent, which
        # would make this test pass for the wrong reason, so both are checked.
        assert await r.exists(key) == 1
        assert await r.pttl(key) == -1


class TestTheRestOfTheSystemSeesTheHalt:
    async def test_the_pre_existing_reader_agrees(self, r: aioredis.Redis) -> None:
        """`state.is_kill_switch_active` is what the risk engine consults, and
        it reads by EXISTENCE. This module writes a JSON record as the value —
        a seam where "wrote a value" and "reads a flag" could easily disagree.
        """
        assert await state.is_kill_switch_active(r, keys.kill_switch()) is False
        await HaltController(r).arm(
            HaltReason.STALE_FEED, detail="no tick for 45s", armed_by="ingest", now=NOW
        )
        assert await state.is_kill_switch_active(r, keys.kill_switch()) is True

    async def test_clearing_is_visible_to_the_pre_existing_reader(self, r: aioredis.Redis) -> None:
        controller = HaltController(r)
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        await controller.clear(KILL_SWITCH, _operator())
        assert await state.is_kill_switch_active(r, keys.kill_switch()) is False

    async def test_a_loss_latch_does_not_arm_the_kill_switch(self, r: aioredis.Redis) -> None:
        """The separation, checked at the key level. If a loss latch also set
        `control:killswitch`, every loss halt would report as a manual stop and
        the three reason codes would collapse into one."""
        await HaltController(r).arm(
            HaltReason.DAILY_LOSS_LIMIT, detail="d", armed_by="risk", now=NOW
        )
        assert await state.is_kill_switch_active(r, keys.kill_switch()) is False
        assert await r.exists(keys.halt_latch(DAILY_LOSS)) == 1


class TestArmingIsIdempotentAgainstTheRealStore:
    async def test_re_arming_does_not_move_the_timestamp(self, r: aioredis.Redis) -> None:
        """A halt that refreshed its own `since` on every trigger would report
        the most recent symptom as the cause, and an incident review would look
        at the wrong minute of the day."""
        controller = HaltController(r)
        first = await controller.arm(
            HaltReason.DAILY_LOSS_LIMIT, detail="down 3.2%", armed_by="risk", now=NOW
        )
        again = await controller.arm(
            HaltReason.DAILY_LOSS_LIMIT,
            detail="down 4.1%",
            armed_by="risk",
            now=NOW + dt.timedelta(minutes=20),
        )
        assert again.since == first.since
        assert again.detail == "down 3.2%"

    async def test_clear_all_releases_every_latch(self, r: aioredis.Redis) -> None:
        controller = HaltController(r)
        await controller.arm(HaltReason.MANUAL, detail="d", armed_by="op", now=NOW)
        await controller.arm(HaltReason.DAILY_LOSS_LIMIT, detail="d", armed_by="r", now=NOW)
        await controller.arm(HaltReason.CONSECUTIVE_LOSS_LIMIT, detail="d", armed_by="r", now=NOW)
        released = await controller.clear_all(_operator())
        assert set(released) == {KILL_SWITCH, DAILY_LOSS, CONSECUTIVE_LOSS}
        assert (await controller.state()).any_halted is False
