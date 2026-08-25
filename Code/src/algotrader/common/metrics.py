"""Process metrics, with one that matters more than the rest.

``LOW_LEVEL_ARCHITECTURE.md §5.7`` calls ``signals_rejected_total{check}`` *the
highest-value debugging metric in the system*, and the reason is worth keeping
in view: it turns "why isn't it trading?" from an investigation into a
dashboard glance. Without it the answer to that question is a log trawl, and
the question gets asked on exactly the days when nobody has time to trawl.

**Everything here is a counter or a gauge, never a decision input.** Metrics
are observation. A control-flow branch that reads a metric would make behaviour
depend on whether scraping happened, which is not a dependency anyone wants in
an order path.

**A registry per process, created once.** ``prometheus_client``'s default
registry is global, and duplicate registration raises — which turns a
double-import into a crash at import time rather than at use. The lazy
singleton below is what stops a test that imports two services from taking the
whole suite down.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cost only
    from prometheus_client import CollectorRegistry

_LOCK = threading.Lock()
_METRICS: Metrics | None = None


class Metrics:
    """The metric objects this process publishes.

    Constructed against an explicit registry rather than the global default so
    tests can build a throwaway instance without colliding with the singleton.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        self.registry = registry if registry is not None else CollectorRegistry()

        # -- risk engine ----------------------------------------------------
        self.signals_rejected_total = Counter(
            "signals_rejected_total",
            "Recommendations rejected by the risk pipeline, by the check that stopped them.",
            labelnames=("check", "reason"),
            registry=self.registry,
        )
        self.signals_approved_total = Counter(
            "signals_approved_total",
            "Recommendations that passed every risk check and were sized.",
            registry=self.registry,
        )
        # Errors are counted SEPARATELY from rejections. A check that threw is
        # not the same event as a check that said no, and merging them would
        # hide a broken check behind a plausible-looking rejection rate.
        self.risk_check_errors_total = Counter(
            "risk_check_errors_total",
            "Risk checks that raised. Such a check ALSO increments "
            "signals_rejected_total — the order really was rejected — but it "
            "needs a signal of its own, because a check that cannot answer is "
            "a fault and a check that says no is not.",
            labelnames=("check",),
            registry=self.registry,
        )
        self.risk_evaluation_seconds = Histogram(
            "risk_evaluation_seconds",
            "Wall time for a full risk evaluation.",
            registry=self.registry,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        self.kill_switch_active = Gauge(
            "kill_switch_active",
            "1 when the kill switch is engaged and no new risk may be taken.",
            registry=self.registry,
        )

    def rejected(self, check: str, reason: str) -> None:
        self.signals_rejected_total.labels(check=check, reason=reason).inc()

    def approved(self) -> None:
        self.signals_approved_total.inc()

    def check_errored(self, check: str) -> None:
        self.risk_check_errors_total.labels(check=check).inc()


def get_metrics() -> Metrics:
    """The process-wide instance, created on first use.

    Double-checked under a lock: two services starting concurrently in the same
    process would otherwise each build a registry, and the second set of
    counters would silently receive none of the increments.
    """
    global _METRICS
    if _METRICS is None:
        with _LOCK:
            if _METRICS is None:
                _METRICS = Metrics()
    return _METRICS


def reset_metrics_for_testing() -> None:
    """Drop the singleton so a test can start from zero.

    Named for what it is. A test asserting a counter went from 0 to 1 is
    otherwise order-dependent, and an order-dependent metric test is the kind
    that passes alone and fails in the suite.
    """
    global _METRICS
    with _LOCK:
        _METRICS = None
