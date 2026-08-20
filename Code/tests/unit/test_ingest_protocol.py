"""Kite wire protocol parsing (E05-S01).

These tests build packets to the documented specification and parse them back.
That proves the parser matches the spec; it does NOT prove the spec matches the
exchange, and the module says so. Before this feeds anything that trades, one
real packet per mode must be captured and asserted against — a transposed field
here yields plausible numbers, which is the failure that survives review.

What IS proved here is the part most likely to be wrong in a hand-written
parser: that a malformed or truncated frame is refused rather than salvaged.
"""

from __future__ import annotations

import struct
from decimal import Decimal

import pytest

from algotrader.ingest import kite_protocol as kp

TOKEN = 408065


def _ltp_packet(token: int = TOKEN, paise: int = 250075) -> bytes:
    return struct.pack(">Ii", token, paise)


def _quote_packet(
    token: int = TOKEN,
    last: int = 250075,
    volume: int = 1_234_567,
    ohlc: tuple[int, int, int, int] = (248000, 251000, 247500, 249000),
) -> bytes:
    return struct.pack(">Iiiiiiiiiii", token, last, 10, 249900, volume, 500, 600, *ohlc)


def _full_packet(token: int = TOKEN, last: int = 250075) -> bytes:
    head = struct.pack(
        ">Iiiiiiiiiii", token, last, 10, 249900, 1_234_567, 500, 600, 248000, 251000, 247500, 249000
    )
    extra = struct.pack(">iiiii", 1_760_000_000, 0, 0, 0, 1_760_000_005)
    depth = b""
    for level in range(5):  # bids, descending
        depth += struct.pack(">iiHH", 100 + level, 250000 - level * 5, 3, 0)
    for level in range(5):  # asks, ascending
        depth += struct.pack(">iiHH", 200 + level, 250100 + level * 5, 4, 0)
    return head + extra + depth


def _frame(*packets: bytes) -> bytes:
    out = struct.pack(">H", len(packets))
    for p in packets:
        out += struct.pack(">H", len(p)) + p
    return out


class TestPacketSizesSelectTheMode:
    def test_an_ltp_packet_is_eight_bytes(self) -> None:
        assert len(_ltp_packet()) == kp.LTP_SIZE
        assert kp.parse_packet(_ltp_packet()).mode is kp.Mode.LTP

    def test_a_quote_packet_is_forty_four_bytes(self) -> None:
        assert len(_quote_packet()) == kp.QUOTE_SIZE
        assert kp.parse_packet(_quote_packet()).mode is kp.Mode.QUOTE

    def test_a_full_packet_is_one_hundred_and_eighty_four_bytes(self) -> None:
        assert len(_full_packet()) == kp.FULL_SIZE
        assert kp.parse_packet(_full_packet()).mode is kp.Mode.FULL

    def test_an_unknown_size_is_refused_rather_than_guessed(self) -> None:
        """A size we do not recognise means the protocol moved or the read is
        misaligned. Interpreting it anyway produces plausible garbage."""
        with pytest.raises(kp.ProtocolError, match="matches no known Kite mode"):
            kp.parse_packet(b"\x00" * 33)


class TestPricesAreExact:
    def test_paise_become_rupees_without_float_error(self) -> None:
        tick = kp.parse_packet(_ltp_packet(paise=250075))
        assert tick.last_price == Decimal("2500.75")
        assert isinstance(tick.last_price, Decimal)

    def test_a_currency_divisor_can_be_supplied(self) -> None:
        """Currencies are quoted to seven places, not two."""
        tick = kp.parse_packet(_ltp_packet(paise=875_000_00), divisor=kp.CURRENCY_DIVISOR)
        assert tick.last_price == Decimal("8.75")

    def test_ohlc_survives_the_round_trip(self) -> None:
        tick = kp.parse_packet(_quote_packet(ohlc=(248000, 251000, 247500, 249000)))
        assert (tick.open_price, tick.high_price, tick.low_price, tick.close_price) == (
            Decimal("2480"),
            Decimal("2510"),
            Decimal("2475"),
            Decimal("2490"),
        )


class TestFullModeDepth:
    def test_five_levels_a_side(self) -> None:
        tick = kp.parse_packet(_full_packet())
        assert len(tick.bids) == kp.DEPTH_LEVELS
        assert len(tick.asks) == kp.DEPTH_LEVELS

    def test_best_bid_and_ask_are_the_first_levels(self) -> None:
        tick = kp.parse_packet(_full_packet())
        assert tick.best_bid == Decimal("2500.00")
        assert tick.best_ask == Decimal("2501.00")

    def test_the_book_is_not_crossed(self) -> None:
        """A crossed book would mean the depth halves are transposed — exactly
        the kind of offset error a synthetic round trip can still catch."""
        tick = kp.parse_packet(_full_packet())
        assert tick.best_bid is not None and tick.best_ask is not None
        assert tick.best_bid < tick.best_ask

    def test_exchange_timestamp_is_timezone_aware_utc(self) -> None:
        import datetime as dt

        tick = kp.parse_packet(_full_packet())
        assert tick.exchange_timestamp is not None
        assert tick.exchange_timestamp.tzinfo is dt.UTC

    def test_a_zero_timestamp_becomes_none_not_the_epoch(self) -> None:
        """1970-01-01 would sail through a staleness window and silently
        reorder every time-sorted view it landed in."""
        assert kp._epoch(0) is None


class TestFrameSplitting:
    def test_a_multi_packet_frame_yields_each_packet(self) -> None:
        ticks = kp.parse_frame(_frame(_ltp_packet(), _quote_packet(), _full_packet()))
        assert [t.mode for t in ticks] == [kp.Mode.LTP, kp.Mode.QUOTE, kp.Mode.FULL]

    def test_an_empty_frame_is_not_an_error(self) -> None:
        assert kp.split_frame(struct.pack(">H", 0)) == []

    def test_a_frame_too_short_for_a_count_is_refused(self) -> None:
        with pytest.raises(kp.ProtocolError, match="too short"):
            kp.split_frame(b"\x00")

    def test_a_frame_claiming_more_packets_than_it_carries_is_refused(self) -> None:
        with pytest.raises(kp.ProtocolError, match="ran out of bytes"):
            kp.split_frame(struct.pack(">H", 3) + struct.pack(">H", 8) + _ltp_packet())

    def test_a_truncated_packet_is_refused_rather_than_padded(self) -> None:
        """A short read that parses successfully builds a packet from whatever
        followed it in the buffer."""
        with pytest.raises(kp.ProtocolError, match="declares 44 bytes"):
            kp.split_frame(struct.pack(">HH", 1, 44) + b"x" * 10)


class TestIndexPackets:
    def test_an_index_quote_carries_no_volume(self) -> None:
        # token, last, high, low, open, close, change — 28 bytes.
        packet = struct.pack(">Iiiiiii", 256265, 2450000, 2460000, 2440000, 2445000, 2448000, 1200)
        assert len(packet) == kp.INDEX_QUOTE_SIZE
        tick = kp.parse_packet(packet)
        assert tick.is_index
        assert tick.volume is None

    def test_an_index_full_carries_a_timestamp(self) -> None:
        # ...plus exchange_timestamp — 32 bytes.
        packet = struct.pack(
            ">Iiiiiiii",
            256265,
            2450000,
            2460000,
            2440000,
            2445000,
            2448000,
            1200,
            1_760_000_000,
        )
        assert len(packet) == kp.INDEX_FULL_SIZE
        assert kp.parse_packet(packet).exchange_timestamp is not None


class TestOutboundMessages:
    def test_subscribe_shape(self) -> None:
        assert kp.subscribe_message([408065, 884737]) == '{"a":"subscribe","v":[408065,884737]}'

    def test_mode_shape(self) -> None:
        assert kp.mode_message(kp.Mode.FULL, [408065]) == '{"a":"mode","v":["full",[408065]]}'

    def test_an_empty_subscription_is_refused(self) -> None:
        """Subscribing to nothing succeeds silently and then never ticks."""
        with pytest.raises(ValueError, match="no instrument tokens"):
            kp.subscribe_message([])

    def test_the_stream_url_requires_both_credentials(self) -> None:
        with pytest.raises(ValueError, match="both api_key and access_token"):
            kp.stream_url("wss://ws.kite.trade", "key", "")

    def test_the_stream_url_carries_both(self) -> None:
        url = kp.stream_url("wss://ws.kite.trade", "abc", "tok")
        assert url == "wss://ws.kite.trade?api_key=abc&access_token=tok"
