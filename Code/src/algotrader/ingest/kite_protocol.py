"""Kite WebSocket wire protocol — parsing, with no socket in sight.

**Why this exists rather than ``KiteTicker``.** The SDK's ticker is built on
``autobahn[twisted]``, which drags a Twisted reactor into an asyncio process
and has to be bridged. The wire protocol is fully documented — endpoint,
subscribe messages, byte-level packet layouts — so implementing it directly
avoids the bridge entirely.

An earlier version of this note also claimed it removed CVE-2020-35678 from the
reachable code. That was WRONG, and the correction is worth keeping: importing
``kiteconnect`` at all executes ``kiteconnect/__init__.py``, which imports
``.ticker`` unconditionally, so autobahn and Twisted load into any process that
touches the broker layer whether or not a ticker is ever constructed. Not using
a package is not the same as not having it. Blocker B7 was closed the only way
it could be — by upgrading autobahn past the CVE (``>=20.12.3`` in
``pyproject.toml``), which kiteconnect 5.2.1 tolerates because its ``==`` pin is
declarative rather than a runtime requirement.

Everything here is a pure function over ``bytes``. That is deliberate: the
parsing is the part most likely to be subtly wrong, and it is the part a socket
makes hardest to test. The client in ``kite_ws.py`` owns the connection and
calls into this.

⚠️  **The offsets below follow Kite Connect v3's documented layout and are
verified here only for self-consistency** — the tests build packets with the
same spec they parse. That proves the parser matches the specification; it does
not prove the specification matches the exchange. Before this feeds anything
that trades, capture one real packet per mode and assert against it. A
transposed field here produces plausible numbers, which is the failure mode
that survives review.
"""

from __future__ import annotations

import datetime as dt
import struct
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: Prices arrive as integer paise. Dividing as ``Decimal`` is exact; dividing as
#: ``float`` is not, and every downstream price would inherit the error.
PAISE: Final = Decimal(100)

#: Currency instruments are quoted to seven places rather than two.
CURRENCY_DIVISOR: Final = Decimal(10_000_000)

#: Packet sizes, in bytes, for the three subscription modes.
LTP_SIZE: Final = 8
QUOTE_SIZE: Final = 44
FULL_SIZE: Final = 184

#: Index packets carry fewer fields — no volume, no depth.
INDEX_QUOTE_SIZE: Final = 28
INDEX_FULL_SIZE: Final = 32

#: One market-depth entry: quantity(i4) price(i4) orders(i2) pad(i2).
DEPTH_ENTRY_SIZE: Final = 12
DEPTH_LEVELS: Final = 5


class Mode(StrEnum):
    """Subscription depth. More data costs more bandwidth per tick."""

    LTP = "ltp"
    QUOTE = "quote"
    FULL = "full"


class ProtocolError(ValueError):
    """A frame could not be parsed. Never guessed around.

    A malformed frame is a signal — a protocol change, a truncated read, or a
    connection that is not what it claims to be. Salvaging what looks parseable
    would feed the indicator engine numbers of unknown provenance.
    """


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """One side of one price level."""

    quantity: int
    price: Decimal
    orders: int


@dataclass(frozen=True, slots=True)
class RawTick:
    """One instrument's update, straight off the wire.

    Deliberately NOT the domain ``Tick``: this is the broker's shape, including
    fields the domain model has no place for. Converting happens in the cleaning
    pipeline, after validation — so a tick that fails a check never becomes a
    domain object at all.
    """

    instrument_token: int
    mode: Mode
    last_price: Decimal
    last_quantity: int | None = None
    average_price: Decimal | None = None
    volume: int | None = None
    total_buy_quantity: int | None = None
    total_sell_quantity: int | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    exchange_timestamp: dt.datetime | None = None
    last_trade_time: dt.datetime | None = None
    oi: int | None = None
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()
    is_index: bool = False

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None


def to_price(paise: int, *, divisor: Decimal = PAISE) -> Decimal:
    """Integer paise -> rupees, exactly."""
    return Decimal(paise) / divisor


def _epoch(seconds: int) -> dt.datetime | None:
    """Exchange timestamps are epoch seconds, and 0 means "not sent".

    Returning ``None`` rather than 1970-01-01 matters: a tick stamped at the
    epoch would sail through a staleness check that compares against a window,
    and would silently reorder every time-sorted view it landed in.
    """
    if seconds <= 0:
        return None
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)


def split_frame(frame: bytes) -> list[bytes]:
    """A binary frame -> its packets.

    Layout: ``int16`` packet count, then for each packet an ``int16`` length
    followed by that many bytes. A frame whose declared lengths do not add up is
    rejected rather than truncated — a short read that parses "successfully"
    yields a packet built from whatever followed it in the buffer.
    """
    if len(frame) < 2:
        raise ProtocolError(f"frame is {len(frame)} bytes; too short to carry a packet count")

    count = struct.unpack_from(">H", frame, 0)[0]
    if count == 0:
        return []

    packets: list[bytes] = []
    offset = 2
    for index in range(count):
        if offset + 2 > len(frame):
            raise ProtocolError(
                f"frame claims {count} packets but ran out of bytes reading the length "
                f"of packet {index + 1}"
            )
        length = struct.unpack_from(">H", frame, offset)[0]
        offset += 2
        if offset + length > len(frame):
            raise ProtocolError(
                f"packet {index + 1} declares {length} bytes but only {len(frame) - offset} remain"
            )
        packets.append(frame[offset : offset + length])
        offset += length
    return packets


def parse_packet(packet: bytes, *, divisor: Decimal = PAISE) -> RawTick:
    """One packet -> a :class:`RawTick`.

    Mode is inferred from length, which is how the protocol distinguishes them.
    An unrecognised length raises: it means either a protocol change or a
    misaligned read, and both are conditions to stop on rather than interpret.
    """
    size = len(packet)
    if size < LTP_SIZE:
        raise ProtocolError(f"packet is {size} bytes; the smallest valid packet is {LTP_SIZE}")

    token = struct.unpack_from(">I", packet, 0)[0]

    # Index packets are shorter than tradable ones of the same mode and carry no
    # volume or depth. Distinguished by length alone.
    if size in (INDEX_QUOTE_SIZE, INDEX_FULL_SIZE):
        return _parse_index(packet, token, divisor)

    if size == LTP_SIZE:
        return RawTick(
            instrument_token=token,
            mode=Mode.LTP,
            last_price=to_price(struct.unpack_from(">i", packet, 4)[0], divisor=divisor),
        )

    if size not in (QUOTE_SIZE, FULL_SIZE):
        raise ProtocolError(
            f"packet is {size} bytes, which matches no known Kite mode "
            f"({LTP_SIZE}/{QUOTE_SIZE}/{FULL_SIZE}, or {INDEX_QUOTE_SIZE}/{INDEX_FULL_SIZE} "
            f"for an index). Refusing to guess at the layout."
        )

    (
        last_price,
        last_quantity,
        average_price,
        volume,
        buy_quantity,
        sell_quantity,
        open_price,
        high_price,
        low_price,
        close_price,
    ) = struct.unpack_from(">iiiiiiiiii", packet, 4)

    tick = RawTick(
        instrument_token=token,
        mode=Mode.QUOTE if size == QUOTE_SIZE else Mode.FULL,
        last_price=to_price(last_price, divisor=divisor),
        last_quantity=last_quantity,
        average_price=to_price(average_price, divisor=divisor),
        volume=volume,
        total_buy_quantity=buy_quantity,
        total_sell_quantity=sell_quantity,
        open_price=to_price(open_price, divisor=divisor),
        high_price=to_price(high_price, divisor=divisor),
        low_price=to_price(low_price, divisor=divisor),
        close_price=to_price(close_price, divisor=divisor),
    )
    if size == QUOTE_SIZE:
        return tick

    last_trade_time, oi, _oi_high, _oi_low, exchange_timestamp = struct.unpack_from(
        ">iiiii", packet, 44
    )
    bids, asks = _parse_depth(packet, divisor)
    return RawTick(
        instrument_token=tick.instrument_token,
        mode=Mode.FULL,
        last_price=tick.last_price,
        last_quantity=tick.last_quantity,
        average_price=tick.average_price,
        volume=tick.volume,
        total_buy_quantity=tick.total_buy_quantity,
        total_sell_quantity=tick.total_sell_quantity,
        open_price=tick.open_price,
        high_price=tick.high_price,
        low_price=tick.low_price,
        close_price=tick.close_price,
        last_trade_time=_epoch(last_trade_time),
        oi=oi,
        exchange_timestamp=_epoch(exchange_timestamp),
        bids=bids,
        asks=asks,
    )


def _parse_index(packet: bytes, token: int, divisor: Decimal) -> RawTick:
    """Index packets: no volume, no depth.

    Layout is token, last, high, low, open, close, change — and for full mode a
    trailing exchange timestamp. ``change`` is read past deliberately: it is
    derivable from close and last, and storing a broker-computed duplicate of a
    value we can compute invites the two to disagree.
    """
    last_price, high, low, open_price, close = struct.unpack_from(">iiiii", packet, 4)
    exchange_timestamp = None
    if len(packet) == INDEX_FULL_SIZE:
        exchange_timestamp = _epoch(struct.unpack_from(">i", packet, 28)[0])
    return RawTick(
        instrument_token=token,
        mode=Mode.QUOTE if len(packet) == INDEX_QUOTE_SIZE else Mode.FULL,
        last_price=to_price(last_price, divisor=divisor),
        high_price=to_price(high, divisor=divisor),
        low_price=to_price(low, divisor=divisor),
        open_price=to_price(open_price, divisor=divisor),
        close_price=to_price(close, divisor=divisor),
        exchange_timestamp=exchange_timestamp,
        is_index=True,
    )


def _parse_depth(
    packet: bytes, divisor: Decimal
) -> tuple[tuple[DepthLevel, ...], tuple[DepthLevel, ...]]:
    """Ten depth entries: five bids then five asks."""
    levels: list[DepthLevel] = []
    offset = 64
    for _ in range(DEPTH_LEVELS * 2):
        quantity, price, orders = struct.unpack_from(">iiH", packet, offset)
        levels.append(
            DepthLevel(quantity=quantity, price=to_price(price, divisor=divisor), orders=orders)
        )
        offset += DEPTH_ENTRY_SIZE
    return tuple(levels[:DEPTH_LEVELS]), tuple(levels[DEPTH_LEVELS:])


def parse_frame(frame: bytes, *, divisor: Decimal = PAISE) -> list[RawTick]:
    """Convenience: a whole binary frame -> ticks."""
    return [parse_packet(p, divisor=divisor) for p in split_frame(frame)]


# ---------------------------------------------------------------------------
# Outbound messages
# ---------------------------------------------------------------------------


def subscribe_message(tokens: list[int]) -> str:
    """``{"a":"subscribe","v":[...]}``"""
    import json

    if not tokens:
        raise ValueError("subscribe called with no instrument tokens")
    return json.dumps({"a": "subscribe", "v": list(tokens)}, separators=(",", ":"))


def unsubscribe_message(tokens: list[int]) -> str:
    import json

    if not tokens:
        raise ValueError("unsubscribe called with no instrument tokens")
    return json.dumps({"a": "unsubscribe", "v": list(tokens)}, separators=(",", ":"))


def mode_message(mode: Mode, tokens: list[int]) -> str:
    """``{"a":"mode","v":["full",[...]]}``"""
    import json

    if not tokens:
        raise ValueError("mode change requested for no instrument tokens")
    return json.dumps({"a": "mode", "v": [mode.value, list(tokens)]}, separators=(",", ":"))


def stream_url(base: str, api_key: str, access_token: str) -> str:
    """The authenticated WebSocket URL.

    Both credentials are query parameters because that is what Kite accepts.
    The caller reveals the token exactly once to build this and does not hold
    the result — see ``broker/kite/session.py`` for the same discipline.
    """
    if not api_key or not access_token:
        raise ValueError("both api_key and access_token are required to open the stream")
    return f"{base}?api_key={api_key}&access_token={access_token}"
