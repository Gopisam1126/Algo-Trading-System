"""Market protection is mandatory on MARKET and SL-M orders.

Zerodha rejects unprotected market orders from 1 April 2026, and explicitly
rejects a protection value of 0. Catching this in the type system matters
because of *when* the failure would otherwise appear: the square-off exit at
15:09 is a market order, so an unprotected one means the position does not
close and the broker force-closes it at whatever price is there.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from algotrader.common.config import ExecutionConfig
from algotrader.common.enums import OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import OrderRequest


def _order(order_type: OrderType, **kw: object) -> OrderRequest:
    base: dict[str, object] = {
        "client_order_id": "a" * 32,
        "correlation_id": uuid4(),
        "symbol": "RELIANCE",
        "side": Side.SELL,
        "order_type": order_type,
        "product": Product.MIS,
        "quantity": 10,
        "intent": OrderIntent.SQUAREOFF,
    }
    if order_type in (OrderType.SL, OrderType.SLM):
        base["trigger_price"] = Decimal("1000")
    if order_type in (OrderType.LIMIT, OrderType.SL):
        base["limit_price"] = Decimal("1000")
    base.update(kw)
    return OrderRequest.model_validate(base)


class TestMarketOrdersRequireProtection:
    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.SLM])
    def test_missing_protection_rejected(self, order_type: OrderType) -> None:
        with pytest.raises(ValidationError, match="require market_protection"):
            _order(order_type)

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.SLM])
    def test_zero_protection_rejected(self, order_type: OrderType) -> None:
        """0 is explicitly rejected by the broker."""
        with pytest.raises(ValidationError, match="0 is explicitly rejected"):
            _order(order_type, market_protection=Decimal("0"))

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.SLM])
    def test_auto_protection_accepted(self, order_type: OrderType) -> None:
        assert _order(order_type, market_protection=Decimal("-1")).market_protection == -1

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.SLM])
    def test_percentage_protection_accepted(self, order_type: OrderType) -> None:
        assert _order(order_type, market_protection=Decimal("2.5")).market_protection == Decimal("2.5")

    def test_other_negatives_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _order(OrderType.MARKET, market_protection=Decimal("-5"))


class TestLimitOrdersUnaffected:
    """Protection applies only to MARKET and SL-M."""

    def test_limit_needs_no_protection(self) -> None:
        assert _order(OrderType.LIMIT).market_protection is None

    def test_sl_limit_needs_no_protection(self) -> None:
        assert _order(OrderType.SL).market_protection is None


class TestConfigLevel:
    def test_default_is_auto(self) -> None:
        assert ExecutionConfig().market_protection == Decimal("-1")

    def test_zero_rejected_in_config(self) -> None:
        with pytest.raises(ValidationError, match="rejected by the broker"):
            ExecutionConfig(market_protection=Decimal("0"))

    def test_stray_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only valid negative"):
            ExecutionConfig(market_protection=Decimal("-3"))

    def test_positive_percentage_allowed(self) -> None:
        assert ExecutionConfig(market_protection=Decimal("1.5")).market_protection == Decimal("1.5")
