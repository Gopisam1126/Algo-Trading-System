#!/usr/bin/env python
"""Backfill daily bars from NSE's bhavcopy for a range of sessions (E03-S01).

    python scripts/backfill_bhavcopy.py --start 2026-01-01 --end 2026-11-20
    python scripts/backfill_bhavcopy.py --start 2026-01-01 --end 2026-11-20 --offline

Every trading day in the range (the holiday calendar decides which) is read
from the archive if it is there, otherwise fetched from NSE's archive at most
once a second, parsed strictly, archived, and stored as ``1d`` bars. Re-running
a range is safe: storage is idempotent, and ``--offline`` proves it by reading
only the archive and never touching the network.

The database URL comes from ``--database-url`` or ``DATABASE_URL``, and must be
the services' login (``algotrader_svc``), which may write bars and cannot
rewrite the audit trail. A login that could - the owner that runs the
migrations - is refused before anything is fetched: the check every service
entrypoint makes (audit SEC-002), because this command writes what it reads
from the internet. The range may not reach a year ``config/nse_holidays.yaml``
does not cover - the calendar refuses rather than guess its holidays.

Exit 0 when every session in the range was stored; 1 when any was missing or
refused (each is named); 2 on a usage error, a login that could rewrite the
record, or a year the holiday list does not cover.
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
DEFAULT_ARCHIVE = CODE / "data" / "archive" / "bhavcopy"


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
    from algotrader.premarket.bhavcopy import (
        BhavcopyArchive,
        BhavcopyLoader,
        DayOutcome,
        DayResult,
        RepositoryBarStore,
    )

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
        loader = BhavcopyLoader(
            calendar=calendar,
            archive=BhavcopyArchive(args.archive),
            store=RepositoryBarStore(db_engine.create_session_factory(engine)),
            offline=args.offline,
        )

        def show(r: DayResult) -> None:
            source = r.source or "-"
            line = f"  {r.day}  {r.outcome.value:<17} {source:<8} bars={r.bars}"
            if r.detail:
                line += f"  {r.detail}"
            print(line, flush=True)

        try:
            report = await loader.backfill(args.start, args.end, progress=show)
        except HolidayDataError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    finally:
        await engine.dispose()
    stored = len(report.of(DayOutcome.STORED))
    missing = len(report.of(DayOutcome.MISSING))
    refused = len(report.of(DayOutcome.REFUSED))
    print(
        f"\n{len(report.results)} session(s): {stored} stored, {missing} missing, "
        f"{refused} refused; {report.bars} bar(s) written"
    )
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--start", type=_date, required=True)
    parser.add_argument("--end", type=_date, required=True)
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
        # shell history; an environment variable is the process's own. Refused
        # rather than warned about, because the URL is already exposed by the
        # time a warning could be read (STRIDE, E03-S01).
        parser.error(
            "--database-url carries a password; set DATABASE_URL instead - a command "
            "line is visible to every process on this host"
        )
    if args.database_url is None:
        args.database_url = os.environ.get("DATABASE_URL", "")
    if not args.database_url:
        parser.error("no database: pass --database-url or set DATABASE_URL")
    if args.end < args.start:
        parser.error(f"--end {args.end} is before --start {args.start}")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
