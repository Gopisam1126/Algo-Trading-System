#!/usr/bin/env python
"""Prove — or disprove — that this host can reach the public data sources.

Blocker B8 says NSE blocks programmatic access to its public data. What is
known: NSE's edge refuses non-Indian addresses, and its home page can refuse an
automated client even from an Indian one - on 23 Nov 2026 it answered 403 to an
Indian ISP's address while every API and archive endpoint this probe and the
data stories use answered the same client, given browser headers and no cookie.
So a 403 from the home page alone is not a verdict on the host.

The production host is planned in an India region - this system's own rule,
not a regulation (audit HAL-001) - and the order path needs a static,
broker-whitelisted IP. Re-run this probe there, because what NSE accepts from
one address says little about another.

    python scripts/check_data_reachability.py

Exit code 0 means every required source answered. Non-zero means at least one
did not, and the output says which and how.

**It reads only. It never places an order and needs no credentials.** Safe to
run on a production host at any time, including during market hours.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: NSE serves 403 to anything that does not look like a browser arriving from
#: its own site. This is not an attempt to evade a block — the site is public
#: and free — it is the handshake its CDN expects, and omitting it produces a
#: "blocked" result that says nothing about whether the host is allowed.
#:
#: Whether sending it meets NSE's terms of use is a judgement for the owner,
#: not for this script (audit NR-012, open): its robots policy excludes
#: crawlers, and this is a handful of requests at 2 a second, run by hand. The
#: egress lookup below also tells api.ipify.org this host's address - which
#: any server it connects to learns anyway.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

#: NseIndiaApi's throttle, adopted here. NSE is a free public service and this
#: is a personal system; the correct request rate is the slowest one that gets
#: the job done.
MAX_REQUESTS_PER_SECOND = 2.0

#: How much of a non-JSON reply is read: enough to know an answer came back.
PEEK_BYTES = 4096
#: How much of a JSON reply may be read to parse it whole. The first version
#: read 4 KB of every reply and parsed that, so a JSON answer longer than 4 KB
#: was reported as "not JSON - an interstitial or block page" from a host that
#: had reached it (audit ENG-014). Capped, so a runaway reply cannot exhaust
#: memory; a reply over the cap fails and says so.
MAX_JSON_BYTES = 8 * 1024 * 1024


@dataclass
class Probe:
    name: str
    url: str
    blocks: str
    required: bool = True
    expect_json: bool = False
    #: False when the URL is a stand-in - a page or API on the same host -
    #: rather than the endpoint the dependent story will fetch. A pass then
    #: proves the host is reachable, not that the endpoint is (audit ENG-041),
    #: and the summary says so instead of declaring B8 resolved.
    is_the_endpoint: bool = True


PROBES = [
    Probe(
        name="NSE home (session cookie)",
        url="https://www.nseindia.com/",
        # Informational, not required (corrected 23 Nov 2026). This said every
        # other NSE probe needed the cookie this page issues; that day the page
        # answered 403 to an Indian host while the ASM and market-status APIs
        # answered it without any cookie. A required probe that is not needed
        # turns a working host into a failing exit code.
        blocks="nothing on its own - the NSE APIs answered without its cookie on 23 Nov 2026",
        required=False,
    ),
    Probe(
        name="NSE all-reports page",
        url="https://www.nseindia.com/all-reports",
        blocks="E03-S01 daily bhavcopy ingest - the page that lists it; the "
        "download URL is E03-S01's to define and probe",
        is_the_endpoint=False,
    ),
    Probe(
        name="NSE ASM list",
        url="https://www.nseindia.com/api/reportASM",
        blocks="E04-S01 surveillance list",
        expect_json=True,
    ),
    Probe(
        name="NSE market status API",
        url="https://www.nseindia.com/api/marketStatus",
        blocks="E04-S03 F&O ban list - an NSE API answering with the session "
        "cookie; the ban-list file is E04-S03's to define and probe",
        expect_json=True,
        is_the_endpoint=False,
    ),
    Probe(
        name="BSE (independent fallback)",
        url="https://www.bseindia.com/",
        blocks="the fallback path if NSE stays unreachable",
        required=False,
    ),
    Probe(
        # Verified 18 Nov 2026 (audit NR-011): an unauthenticated GET answers
        # HTTP 200 with the ~10 MB CSV, so a 401 here is a real change, not a
        # missing header.
        name="Kite instruments dump",
        url="https://api.kite.trade/instruments",
        blocks="E02-S06 instrument master (needs no credential)",
    ),
]


@dataclass
class Result:
    probe: Probe
    ok: bool
    detail: str
    seconds: float = 0.0


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)
        mark = "PASS" if result.ok else ("FAIL" if result.probe.required else "warn")
        print(f"  [{mark}] {result.probe.name:32} {result.detail} ({result.seconds:.2f}s)")

    @property
    def blocked(self) -> list[Result]:
        return [r for r in self.results if not r.ok and r.probe.required]


def _egress_ip() -> str:
    """The address a broker would whitelist, and the one NSE geolocates."""
    try:
        request = urllib.request.Request(
            "https://api.ipify.org?format=json", headers=BROWSER_HEADERS
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return str(json.loads(response.read()).get("ip", "unknown"))
    except Exception as exc:  # pragma: no cover - network dependent
        return f"undetermined ({type(exc).__name__})"


def _probe(probe: Probe, opener: urllib.request.OpenerDirector) -> Result:
    started = time.monotonic()
    if not probe.url.startswith("https://"):
        # S310 guard, and a real one: every probe target is a fixed https URL
        # declared in PROBES above. Checking it here means a future edit that
        # introduced a file: or custom scheme fails instead of opening it.
        return Result(probe, False, f"refusing non-https URL: {probe.url!r}")
    request = urllib.request.Request(probe.url, headers=BROWSER_HEADERS)  # noqa: S310
    try:
        with opener.open(request, timeout=20) as response:
            limit = MAX_JSON_BYTES if probe.expect_json else PEEK_BYTES
            body = response.read(limit + 1 if probe.expect_json else limit)
            elapsed = time.monotonic() - started
            if probe.expect_json:
                if len(body) > MAX_JSON_BYTES:
                    return Result(
                        probe,
                        False,
                        f"HTTP {response.status} but the reply exceeds {MAX_JSON_BYTES} "
                        f"bytes - not parsed",
                        elapsed,
                    )
                try:
                    json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    return Result(
                        probe,
                        False,
                        f"HTTP {response.status} but the body is not JSON — usually an "
                        f"interstitial or block page",
                        elapsed,
                    )
            return Result(probe, True, f"HTTP {response.status}, {len(body)}+ bytes", elapsed)
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        hint = {
            401: "authentication required",
            403: "FORBIDDEN — NSE refused this client: a non-Indian address, or bot "
            "protection (an Indian host got this from NSE's home page on 23 Nov 2026)",
            429: "rate limited — slow down and retry",
            503: "service unavailable, possibly a temporary block",
        }.get(exc.code, "")
        return Result(probe, False, f"HTTP {exc.code} {hint}".strip(), elapsed)
    except urllib.error.URLError as exc:
        return Result(probe, False, f"unreachable: {exc.reason}", time.monotonic() - started)
    except TimeoutError:
        return Result(probe, False, "timed out", time.monotonic() - started)
    except Exception as exc:  # pragma: no cover - defensive
        return Result(probe, False, f"{type(exc).__name__}: {exc}", time.monotonic() - started)


def summary_when_reachable(report: Report) -> str:
    """What a clean run proves - and, by name, what it does not."""
    stand_ins = [
        r.probe for r in report.results if r.probe.required and not r.probe.is_the_endpoint
    ]
    lines = ["All required probes answered from THIS host."]
    if stand_ins:
        lines.append(
            "B8 is NOT yet resolved: these probes reached a stand-in on the right host, "
            "not the endpoint the story will fetch:"
        )
        lines.extend(f"  - {p.name}: {p.blocks}" for p in stand_ins)
        lines.append("Probe those endpoints when their stories define them.")
    else:
        lines.append("B8 is resolved for THIS host.")
    lines.append("Record the egress IP above in the tracker and whitelist it with the broker.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    print("Data-source reachability probe (read-only, no credentials)\n")
    ip = _egress_ip()
    print(f"  egress IP: {ip}")
    print("  This is the address a broker whitelists and the one NSE geolocates.")
    print("  NSE refuses non-Indian addresses, and can refuse an automated client")
    print("  from an Indian one.\n")

    # A cookie jar. NSE's APIs once wanted the session its home page issues; on
    # 23 Nov 2026 they answered without it, but the jar costs nothing and keeps
    # a return of that requirement from reading as a block.
    import http.cookiejar

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    report = Report()
    interval = 1.0 / MAX_REQUESTS_PER_SECOND
    for probe in PROBES:
        report.add(_probe(probe, opener))
        time.sleep(interval)

    print()
    if args.json:
        print(
            json.dumps(
                {
                    "egress_ip": ip,
                    "results": [
                        {
                            "name": r.probe.name,
                            "ok": r.ok,
                            "required": r.probe.required,
                            "detail": r.detail,
                            "blocks": r.probe.blocks,
                        }
                        for r in report.results
                    ],
                },
                indent=2,
            )
        )

    blocked = report.blocked
    if not blocked:
        print(summary_when_reachable(report))
        return 0

    print(f"{len(blocked)} required source(s) unreachable from this host:\n")
    for result in blocked:
        print(f"  - {result.probe.name}: {result.detail}")
        print(f"    blocks: {result.probe.blocks}")
    print(
        "\nIf these are 403s and the egress IP above is outside India, that is the\n"
        "expected result: re-run this on the India-region production host (an\n"
        "India region is this system's own rule, not a regulation - HAL-001).\n"
        "From an Indian address a 403 means NSE refused the client, not the\n"
        "country: check the request headers before blaming the host."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
