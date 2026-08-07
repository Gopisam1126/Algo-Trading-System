"""Event-loop compatibility shim for async psycopg on Windows.

**The problem this solves, because it is not obvious from the traceback.**

From Python 3.8, Windows' default asyncio policy builds a ``ProactorEventLoop``.
Async psycopg refuses to run on it and raises::

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
    in async mode.

The failure appears at the first ``await`` against the database, which means it
surfaces as a connection error a long way from its cause. It is also invisible
on Linux and macOS — including in CI — so it is exactly the kind of thing that
gets discovered on a developer's machine at the worst moment.

Every async entry point that touches PostgreSQL on Windows must call
:func:`configure_event_loop_policy` **before** the loop is created — that is,
before ``asyncio.run()``, not inside the coroutine.

On non-Windows platforms this is a no-op, and the project's ``uvloop``
dependency (installed only off Windows) is fully compatible with psycopg.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop_policy() -> None:
    """Select an event loop async psycopg can actually use.

    Idempotent and safe to call more than once. Must be called before the
    event loop is created.
    """
    # Written as a positive `== "win32"` check rather than an early
    # `!= "win32": return`. mypy understands `sys.platform` comparisons and
    # narrows on them, so under `--platform linux` everything after the early
    # return is provably dead and `warn_unreachable` fails the build. The
    # positive form is the idiom mypy expects: the block is simply not analysed
    # off Windows, and `WindowsSelectorEventLoopPolicy` — which does not exist
    # on Linux — is never referenced there.
    if sys.platform == "win32":
        # WindowsSelectorEventLoopPolicy is the supported loop for psycopg
        # async. The trade-off is that the selector loop does not support
        # subprocesses on Windows; nothing in the data path needs them.
        current = asyncio.get_event_loop_policy()
        if not isinstance(current, asyncio.WindowsSelectorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
