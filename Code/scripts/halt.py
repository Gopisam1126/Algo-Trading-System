"""The operator's kill switch: read, arm and clear the trading halt.

Audit RSK-016 (10 Nov 2026): no production code armed a MANUAL halt or cleared
any latch, so the kill switch had no human trigger - only ``redis-cli`` against
the raw keys, which bypasses ``OperatorAction`` and the record of who did what.
This is that trigger, until the phone and dashboard transports exist (E14-S12).

    python scripts/halt.py status
    python scripts/halt.py arm   --operator "<your name>" --detail "<why>"
    python scripts/halt.py clear --operator "<your name>" --acknowledge "<what you checked>"
                                 [--latch kill_switch|daily_loss|consecutive_loss|all]

**Arming never closes a position**, and neither does clearing: a halt stops
NEW entries while every exit path keeps running (``execution/halt.py``).
Clearing requires a named person and a statement of what was checked, because
``OperatorAction`` refuses to be built without both. Run it where the Redis
password is available (``REDIS_PASSWORD``, or ``REDIS_URL``), as the service
does - it talks to the same latches the risk engine reads.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from typing import Any

from algotrader.execution.halt import (
    CONSECUTIVE_LOSS,
    DAILY_LOSS,
    KILL_SWITCH,
    HaltController,
    HaltReason,
    OperatorAction,
)

LATCHES = (KILL_SWITCH, DAILY_LOSS, CONSECUTIVE_LOSS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read, arm or clear the trading halt.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show every latch and why it is armed")
    arm = sub.add_parser("arm", help="halt new entries now (MANUAL)")
    arm.add_argument("--operator", required=True, help="who is halting")
    arm.add_argument("--detail", required=True, help="why")
    clear = sub.add_parser("clear", help="release a latch - a human decision")
    clear.add_argument("--operator", required=True, help="who is clearing")
    clear.add_argument("--acknowledge", required=True, help="what you checked before clearing")
    clear.add_argument("--latch", choices=(*LATCHES, "all"), default="all")
    return parser


async def run(argv: Sequence[str], controller: Any, *, now: dt.datetime) -> int:
    """The command, against ``controller``. Returns the process exit code."""
    args = _parser().parse_args(list(argv))
    if args.command == "status":
        state = await controller.state()
        print(state.describe())
        return 1 if state.any_halted else 0
    if args.command == "arm":
        record = await controller.arm(
            HaltReason.MANUAL, detail=args.detail, armed_by=args.operator, now=now
        )
        print(f"halted: {record.reason.value} since {record.since.isoformat()} ({record.detail})")
        return 0
    try:
        action = OperatorAction(operator=args.operator, acknowledged=args.acknowledge)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.latch == "all":
        released = await controller.clear_all(action)
    else:
        released = (args.latch,) if await controller.clear(args.latch, action) else ()
    print(f"released: {', '.join(released) or 'nothing was armed'}")
    print((await controller.state()).describe())
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - wires real Redis
    from algotrader.common.config import load_config
    from algotrader.common.redis.client import build_client, close
    from algotrader.common.secrets import build_provider

    config = load_config()
    secret = build_provider().get_optional("REDIS_PASSWORD")

    async def go() -> int:
        client = build_client(config.redis, secret.reveal() if secret else None)
        try:
            return await run(
                sys.argv[1:] if argv is None else argv,
                HaltController(client),
                now=dt.datetime.now(dt.UTC),
            )
        finally:
            await close(client)

    return asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
