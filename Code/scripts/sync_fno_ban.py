#!/usr/bin/env python
"""Write a trade date's ``is_fno_ban`` flags from NSE's F&O ban list (E04-S03).

    python scripts/sync_fno_ban.py --date 2026-11-27
    python scripts/sync_fno_ban.py --start 2026-01-02 --end 2026-11-27
    python scripts/sync_fno_ban.py --date 2026-11-27 --offline

Each trade date T (the holiday calendar decides which days are traded) reads
the list NSE wrote for T - ``fo_secban_<DDMMYYYY>.csv``, named for the day it
governs and written the evening before - from the archive if it is there,
otherwise from NSE's archive at most once a second; checks when NSE wrote it,
that it is whole and names T, parses it strictly, archives it, and writes
``is_fno_ban`` for every active instrument in the master: true for the
instruments of a security in ban, false for every other. A day that is MISSING
or REFUSED writes nothing, so its flag stays unknown and no symbol can be
traded on it. Re-running is safe: the same list writes the same values, and
``--offline`` reads only the archive and never touches the network.

The database URL comes from ``--database-url`` or ``DATABASE_URL``, and must be
the services' login (``algotrader_svc``). A login that could rewrite the audit
trail - the owner that runs the migrations - is refused before anything is
fetched, as every service entrypoint refuses it (audit SEC-002).

Exit 0 when every session's flags were written; 1 when any was missing or
refused (each is named); 2 on a usage error, a login that could rewrite the
record, or a date whose previous session is in a year the holiday list does
not cover (the first session of 2026 follows the last of 2025: E04-S09).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import urllib.parse
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = CODE / "data" / "archive" / "fno_ban"


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None


async def _run(args: argparse.Namespace) -> int:
    from algotrader.common.calendar import (
        HolidayDataError,
        MarketCalendar,
        load_holidays_with_status,
    )
    from algotrader.common.db import engine as db_engine
    from algotrader.common.db.privilege import LeastPrivilegeError, require_least_privilege
    from algotrader.premarket.fno_ban import (
        FnoBanArchive,
        FnoBanResult,
        FnoBanSync,
        RepositoryBanStore,
    )
    from algotrader.premarket.surveillance import SyncOutcome

    status = load_holidays_with_status(str(CODE / "config" / "nse_holidays.yaml"))
    calendar = MarketCalendar(status.dates, covers_years=status.covers_years)
    engine = db_engine.create_engine_from_url(args.database_url)
    try:
        try:
            async with engine.connect() as conn:
                await require_least_privilege(conn)
        except LeastPrivilegeError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        factory = db_engine.create_session_factory(engine)
        sync = FnoBanSync(
            calendar=calendar,
            archive=FnoBanArchive(args.archive),
            store=RepositoryBanStore(factory),
            offline=args.offline,
        )

        def show(r: FnoBanResult) -> None:
            line = (
                f"  {r.trade_date}  {r.outcome.value:<17} {r.source or '-':<8} "
                f"written={r.written} banned={len(r.banned)} flagged={r.flagged}"
            )
            if r.banned:
                line += f" ({', '.join(r.banned)})"
            if r.detail:
                line += f"  {r.detail}"
            print(line, flush=True)

        try:
            report = await sync.backfill(args.start, args.end, progress=show)
        except HolidayDataError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    finally:
        await engine.dispose()
    stored = len(report.of(SyncOutcome.STORED))
    missing = len(report.of(SyncOutcome.MISSING))
    refused = len(report.of(SyncOutcome.REFUSED))
    written = sum(r.written for r in report.results)
    print(
        f"\n{len(report.results)} session(s): {stored} stored, {missing} missing, "
        f"{refused} refused; {written} status row(s) written"
    )
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--date", type=_date, help="one trade date (= --start D --end D)")
    parser.add_argument("--start", type=_date)
    parser.add_argument("--end", type=_date)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--offline", action="store_true", help="read the archive only")
    parser.add_argument(
        "--database-url",
        default=None,
        help="a URL WITHOUT a password (use DATABASE_URL for one that carries it)",
    )
    args = parser.parse_args(argv)
    if args.database_url is not None and urllib.parse.urlsplit(args.database_url).password:
        # A command line is readable by every process on the host and lands in
        # shell history (QA-SEC-43, as for the bhavcopy backfill).
        parser.error(
            "--database-url carries a password; set DATABASE_URL instead - a command "
            "line is visible to every process on this host"
        )
    if args.date is not None:
        if args.start is not None or args.end is not None:
            parser.error("--date is one day; give it alone, or --start and --end")
        args.start = args.end = args.date
    elif args.start is None or args.end is None:
        parser.error("give --date, or both --start and --end")
    if args.end < args.start:
        parser.error(f"--end {args.end} is before --start {args.start}")
    if args.database_url is None:
        args.database_url = os.environ.get("DATABASE_URL", "")
    if not args.database_url:
        parser.error("no database: pass --database-url or set DATABASE_URL")
    from algotrader.common.db.eventloop import configure_event_loop_policy

    # Before the loop exists: on Windows asyncio.run builds a ProactorEventLoop,
    # which async psycopg refuses at the first query (SIT-011).
    configure_event_loop_policy()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
