"""A download that fails half-way is a refusal, never a crash (QA-E04-12).

``urlopen`` wraps a failure to CONNECT in ``URLError``; a failure while READING
the body - a timeout, a reset, a short chunked read - arrives as a bare
``OSError`` or ``http.client.HTTPException``, which none of the premarket
fetchers caught. Each escaped as a raw exception: the sync crashed with a
traceback instead of reporting the day REFUSED, and a backfill stopped at that
day. It failed closed (nothing was written), so it cost a report, not money -
but it was one defect in every fetcher, so this test holds all of them, the way
``test_csv_parsing.py`` holds every parser (QA-SEC-45).
"""

from __future__ import annotations

import datetime as dt
import http.client
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from algotrader.premarket import bhavcopy, fno_ban, surveillance, universe

DAY = dt.date(2026, 11, 26)

#: Each fetcher, called with a given opener, and the refusal type it owns.
FETCHERS: dict[str, tuple[Callable[[Any], object], type[ValueError]]] = {
    "bhavcopy": (lambda op: bhavcopy.fetch_bhavcopy(DAY, opener=op), bhavcopy.BhavcopyError),
    "roster": (lambda op: surveillance.fetch_roster(DAY, opener=op), surveillance.RosterError),
    "index": (
        lambda op: universe.fetch_index_file("nifty50", opener=op),
        universe.UniverseSourceError,
    ),
    "ban list": (lambda op: fno_ban.fetch_ban_list(DAY, opener=op), fno_ban.BanListError),
}

FAILURES = [
    TimeoutError("timed out"),
    ConnectionResetError(10054, "An existing connection was forcibly closed"),
    http.client.IncompleteRead(b"partial", 100),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
]


class _FailingRead:
    status = 200
    headers: ClassVar[dict[str, str]] = {
        "Last-Modified": "Thu, 26 Nov 2026 13:56:04 GMT",
        "Content-Length": "100",
    }

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def read(self, limit: int = -1) -> bytes:
        raise self._failure

    def __enter__(self) -> _FailingRead:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize("name", sorted(FETCHERS))
@pytest.mark.parametrize("failure", FAILURES, ids=lambda f: type(f).__name__)
def test_a_failure_while_reading_is_the_fetchers_own_refusal(
    name: str, failure: BaseException
) -> None:
    call, refusal = FETCHERS[name]
    with pytest.raises(refusal, match="could not be fetched") as caught:
        call(lambda request, timeout: _FailingRead(failure))
    # Named, so an operator can tell a timeout from a reset; the cause kept.
    assert type(failure).__name__ in str(caught.value)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("name", sorted(FETCHERS))
def test_control_a_body_that_reads_is_not_refused_as_unfetchable(name: str) -> None:
    """The control: the same opener with a body that reads. Each fetcher may
    still refuse it on its own grounds (it is not a real file), but never as
    'could not be fetched'."""

    class _Reads(_FailingRead):
        def read(self, limit: int = -1) -> bytes:
            return b"x" * 100

    call, refusal = FETCHERS[name]
    try:
        call(lambda request, timeout: _Reads(TimeoutError()))
    except refusal as exc:
        assert "could not be fetched" not in str(exc)
