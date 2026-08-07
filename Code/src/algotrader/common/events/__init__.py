"""Event streams — the asynchronous spine between services.

Delivery is at-least-once; every consumer must be idempotent. See
:mod:`~algotrader.common.events.stream` for why that is the correct choice here
rather than a limitation to work around.
"""

from algotrader.common.events.envelope import CURRENT_SCHEMA_VERSION, Envelope
from algotrader.common.events.stream import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAXLEN,
    DEFAULT_MIN_IDLE_MS,
    Delivery,
    StreamError,
    acknowledge,
    consume,
    dead_letter,
    default_maxlen,
    delivery_attempts,
    ensure_group,
    new_consumer_name,
    pending_count,
    publish,
    reclaim_stalled,
    stream_length,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_MAXLEN",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MIN_IDLE_MS",
    "Delivery",
    "Envelope",
    "StreamError",
    "acknowledge",
    "consume",
    "dead_letter",
    "default_maxlen",
    "delivery_attempts",
    "ensure_group",
    "new_consumer_name",
    "pending_count",
    "publish",
    "reclaim_stalled",
    "stream_length",
]
