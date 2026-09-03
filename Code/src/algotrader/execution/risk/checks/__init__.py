"""The fourteen risk checks (E14-S02..S06).

Grouped by what they interrogate rather than by number, because the grouping is
what determines their order: pre-conditions ask whether to trade at all, symbol
eligibility asks about this instrument, portfolio checks ask about the book,
and loss/margin checks ask about capital.
"""

from algotrader.execution.risk.checks.eligibility import (
    ELIGIBILITY_ORDER,
    MAX_RESTRICTIONS_NAMED,
    NOT_ALREADY_HELD_CHECK,
    SLOT_AVAILABLE_CHECK,
    SYMBOL_TRADABLE_CHECK,
    build_eligibility_checks,
    check_slot_available,
    check_symbol_not_already_held,
    check_symbol_tradable,
)
from algotrader.execution.risk.checks.exposure import (
    EXPOSURE_ORDER,
    MAX_SYMBOLS_NAMED,
    build_correlation_check,
    build_exposure_checks,
    build_net_exposure_check,
    build_sector_exposure_check,
)
from algotrader.execution.risk.checks.preconditions import (
    HEALTH_GATE_CHECK,
    KILL_SWITCH_CHECK,
    PRECONDITION_ORDER,
    build_no_trade_window_check,
    build_precondition_checks,
    build_trading_window_check,
    check_health_gate,
    check_kill_switch,
    validate_no_trade_windows,
)

__all__ = [
    "ELIGIBILITY_ORDER",
    "EXPOSURE_ORDER",
    "HEALTH_GATE_CHECK",
    "KILL_SWITCH_CHECK",
    "MAX_RESTRICTIONS_NAMED",
    "MAX_SYMBOLS_NAMED",
    "NOT_ALREADY_HELD_CHECK",
    "PRECONDITION_ORDER",
    "SLOT_AVAILABLE_CHECK",
    "SYMBOL_TRADABLE_CHECK",
    "build_correlation_check",
    "build_eligibility_checks",
    "build_exposure_checks",
    "build_net_exposure_check",
    "build_no_trade_window_check",
    "build_precondition_checks",
    "build_sector_exposure_check",
    "build_trading_window_check",
    "check_health_gate",
    "check_kill_switch",
    "check_slot_available",
    "check_symbol_not_already_held",
    "check_symbol_tradable",
    "validate_no_trade_windows",
]
