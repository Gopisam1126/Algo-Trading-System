"""The rolling correlation matrix (E14-S04, task 2).

A statistical routine is the easiest place in this system to write something
that returns a plausible number and is wrong, because nothing downstream can
tell. So these tests check against values that can be derived by hand or by an
independent implementation, not against whatever the code happened to produce.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.execution.risk.correlation import (
    CORRELATION_WINDOW_SESSIONS,
    MIN_SESSIONS_FOR_CORRELATION,
    CorrelationError,
    correlations_against,
    log_returns,
    pearson,
)


def _series(values: list[str]) -> list[Decimal]:
    return [Decimal(v) for v in values]


def _walk(start: float, steps: list[float]) -> list[Decimal]:
    """A close series built from explicit multiplicative steps."""
    out = [Decimal(str(start))]
    price = start
    for s in steps:
        price *= 1 + s
        out.append(Decimal(str(round(price, 4))))
    return out


class TestLogReturns:
    def test_a_known_pair(self) -> None:
        """100 -> 110 is ln(1.1)."""
        (r,) = log_returns(_series(["100", "110"]))
        assert r == pytest.approx(math.log(1.1))

    def test_they_are_additive_across_time(self) -> None:
        """The property log returns are chosen FOR: the sum of the steps
        equals the return of the whole move. Simple returns do not do this,
        and the difference is what biases correlation between a volatile name
        and a quiet one."""
        closes = _series(["100", "110", "99", "123.75"])
        total = sum(log_returns(closes))
        assert total == pytest.approx(math.log(float(closes[-1]) / float(closes[0])))

    def test_a_rise_and_the_matching_fall_cancel(self) -> None:
        """+10% then -10% back to the start nets to zero. In simple returns it
        does not, and that asymmetry is a real source of spurious correlation."""
        assert sum(log_returns(_series(["100", "110", "100"]))) == pytest.approx(0.0)

    @pytest.mark.parametrize("bad", [["100", "0"], ["0", "100"], ["100", "-5"]])
    def test_a_non_positive_close_is_refused(self, bad: list[str]) -> None:
        """A zero close is bad data, not a 100% loss. Computing ln(0) would
        either explode or, worse, produce -inf and poison the whole matrix."""
        with pytest.raises(CorrelationError, match="non-positive"):
            log_returns(_series(bad))

    def test_one_close_cannot_form_a_return(self) -> None:
        with pytest.raises(CorrelationError, match="at least 2"):
            log_returns(_series(["100"]))


class TestPearson:
    def test_a_series_correlates_perfectly_with_itself(self) -> None:
        xs = [float(i % 7) - 3 for i in range(40)]
        assert pearson(xs, xs) == pytest.approx(1.0)

    def test_a_negated_series_correlates_at_minus_one(self) -> None:
        xs = [float(i % 7) - 3 for i in range(40)]
        assert pearson(xs, [-x for x in xs]) == pytest.approx(-1.0)

    def test_it_agrees_with_the_standard_library(self) -> None:
        """The independent implementation. `statistics.correlation` is not used
        in the module — checking against it is what makes this a verification
        rather than a restatement of the same arithmetic."""
        xs = [math.sin(i / 3) for i in range(50)]
        ys = [math.cos(i / 4) + 0.3 * math.sin(i / 3) for i in range(50)]
        assert pearson(xs, ys) == pytest.approx(statistics.correlation(xs, ys), abs=1e-9)

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(CorrelationError, match="lengths differ"):
            pearson([1.0] * 40, [1.0] * 39)

    def test_a_thin_series_is_refused_rather_than_estimated(self) -> None:
        """Below the floor the estimate is noise wearing a number — and a
        number is far more dangerous than a gap, because the guard acts on it."""
        n = MIN_SESSIONS_FOR_CORRELATION - 1
        with pytest.raises(CorrelationError, match="floor"):
            pearson([float(i) for i in range(n)], [float(i) for i in range(n)])

    def test_the_floor_itself_is_allowed(self) -> None:
        n = MIN_SESSIONS_FOR_CORRELATION
        assert pearson([float(i) for i in range(n)], [float(i) for i in range(n)]) == (
            pytest.approx(1.0)
        )

    def test_a_flat_series_is_refused_not_reported_as_zero(self) -> None:
        """0.0 would read as "independent" — a confident claim in place of a
        missing one, which the caller would act on."""
        flat = [1.0] * 40
        varying = [float(i) for i in range(40)]
        with pytest.raises(CorrelationError, match="zero variance"):
            pearson(flat, varying)

    @settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.floats(min_value=-0.2, max_value=0.2, allow_nan=False, allow_infinity=False),
            min_size=MIN_SESSIONS_FOR_CORRELATION,
            max_size=120,
        )
    )
    def test_the_result_is_always_within_bounds(self, xs: list[float]) -> None:
        """The property that matters downstream: the check compares |rho|
        against a threshold in (0, 1]. A value outside [-1, 1] — which naive
        floating-point accumulation can produce — would make the comparison
        meaningless."""
        ys = [x * 2 + 0.01 for x in xs]
        try:
            rho = pearson(xs, ys)
        except CorrelationError:
            return  # zero-variance input, correctly refused
        assert -1.0 <= rho <= 1.0

    #: A daily log return, at magnitudes a real one actually takes.
    #:
    #: CORR-002. This was ``st.floats(min_value=-0.2, max_value=0.2)``, which
    #: also admits values like 1e-160 — and a series of those has a variance so
    #: small that ``rho`` is computed from pure rounding noise. Scaling
    #: invariance is a real property of the *mathematics* and simply cannot
    #: hold there: a sweep of the denormal band finds the invariance error
    #: reaching 1.0, meaning rho flips between 0 and ±1 on a rescale.
    #:
    #: The test was therefore asserting a property outside the domain where it
    #: can hold, and had been falsifiable since it was written — Hypothesis had
    #: simply not drawn that far into the tail. It surfaced during E14-S10,
    #: when CORR-001's fix made three times as many of those inputs computable
    #: instead of refused, which raised the chance of drawing one rather than
    #: introducing the weakness.
    #:
    #: Bounding the strategy is the fix, not loosening the tolerance: a 0.02%
    #: move is a real market day, a 1e-160 move is not any market day, and a
    #: tolerance wide enough to accommodate noise would stop catching the
    #: catastrophic-cancellation bug this test exists to catch.
    A_REAL_RETURN = st.one_of(
        st.just(0.0),  # a genuinely flat day, which does happen
        st.floats(min_value=1e-6, max_value=0.2, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-0.2, max_value=-1e-6, allow_nan=False, allow_infinity=False),
    )

    @settings(max_examples=100, deadline=None)
    @given(
        st.lists(
            A_REAL_RETURN,
            min_size=MIN_SESSIONS_FOR_CORRELATION,
            max_size=80,
        ),
        st.floats(min_value=0.1, max_value=10.0),
        st.floats(min_value=-5.0, max_value=5.0),
    )
    def test_it_is_invariant_to_positive_scaling_and_shift(
        self, xs: list[float], scale: float, shift: float
    ) -> None:
        """Correlation measures shape, not magnitude. A stock that moves twice
        as far in the same pattern is perfectly correlated, not more so — and
        if this were false, the guard would read expensive names as more
        correlated than cheap ones."""
        ys = [math.sin(i / 5) for i in range(len(xs))]
        try:
            base = pearson(xs, ys)
            scaled = pearson([x * scale + shift for x in xs], ys)
        except CorrelationError:
            return
        assert base == pytest.approx(scaled, abs=1e-6)


class TestCorrelationsAgainst:
    @staticmethod
    def _book(n: int = CORRELATION_WINDOW_SESSIONS + 1) -> dict[str, list[Decimal]]:
        rise = [0.01 if i % 2 else -0.005 for i in range(n)]
        return {
            "SBIN": _walk(100, rise),
            "PNB": _walk(50, rise),  # same shape -> perfectly correlated
            "TCS": _walk(3000, [-s for s in rise]),  # mirrored -> -1
        }

    def test_the_shape_is_symbol_to_correlation(self) -> None:
        out = correlations_against("SBIN", self._book(), against=["PNB", "TCS"])
        assert set(out) == {"PNB", "TCS"}
        assert all(isinstance(v, Decimal) for v in out.values())

    def test_an_identical_shape_correlates_at_one(self) -> None:
        out = correlations_against("SBIN", self._book(), against=["PNB"])
        assert out["PNB"] == pytest.approx(Decimal("1"), abs=Decimal("0.0001"))

    def test_a_mirrored_shape_correlates_at_minus_one(self) -> None:
        out = correlations_against("SBIN", self._book(), against=["TCS"])
        assert out["TCS"] == pytest.approx(Decimal("-1"), abs=Decimal("0.0001"))

    def test_an_uncomputable_symbol_is_absent_not_zero(self) -> None:
        """The contract the whole guard rests on. A zero here would read as
        "uncorrelated" and admit the fourth PSU bank; absence reads as
        "unknown" and the check refuses."""
        book = self._book()
        book["THIN"] = _walk(100, [0.01] * 5)  # far too few sessions
        out = correlations_against("SBIN", book, against=["PNB", "THIN"])
        assert "PNB" in out
        assert "THIN" not in out, "an uncomputable correlation must not appear at all"

    def test_a_symbol_with_no_series_is_absent(self) -> None:
        out = correlations_against("SBIN", self._book(), against=["PNB", "NEVERHEARDOF"])
        assert "NEVERHEARDOF" not in out

    def test_a_missing_candidate_series_raises(self) -> None:
        """Different from a missing counterparty: without the candidate there
        is no row to compute at all, and silently returning {} would look like
        'nothing correlated'."""
        with pytest.raises(CorrelationError, match="candidate"):
            correlations_against("UNKNOWN", self._book(), against=["PNB"])

    def test_only_the_window_is_used(self) -> None:
        """A long history must not quietly widen the estimate — the window is
        the design decision, and a series that happens to be longer would
        otherwise change what 'correlated' means for that symbol."""
        long_book = self._book(n=400)
        short_book = {k: v[-(CORRELATION_WINDOW_SESSIONS + 1) :] for k, v in long_book.items()}
        assert correlations_against("SBIN", long_book, against=["PNB"]) == (
            correlations_against("SBIN", short_book, against=["PNB"])
        )

    def test_it_is_deterministic(self) -> None:
        book = self._book()
        assert correlations_against("SBIN", book, against=["PNB", "TCS"]) == (
            correlations_against("SBIN", book, against=["PNB", "TCS"])
        )

    def test_the_window_default_is_the_documented_one(self) -> None:
        """The number is a recorded judgement, not an accident. If someone
        changes it, this test makes them change the design record too."""
        assert CORRELATION_WINDOW_SESSIONS == 60
        assert MIN_SESSIONS_FOR_CORRELATION == 30


class TestTheDenominatorCannotUnderflowToZero:
    """CORR-001, found by this file's own property test during E14-S10.

    ``pearson`` guards against a flat series::

        if var_l <= 0 or var_r <= 0:
            raise CorrelationError(...)

    and that guard is correct as far as it goes. But the division below it used
    ``math.sqrt(var_l * var_r)``, and **the product of two strictly positive
    variances can underflow to 0.0** even when neither operand does. A real
    case: ``var_l = 9.667e-293`` and ``var_r = 3.611e-34`` are both comfortably
    positive; their product is not representable, ``sqrt`` returns 0.0, and the
    division raises ``ZeroDivisionError``.

    **Guarding the operands does not guard the expression.**

    Why it mattered beyond tidiness: ``correlations_against`` catches
    ``CorrelationError`` and only that, deliberately — an uncomputable pair is
    *absent* from the result, and the check reads absence as "unknown" and
    refuses. A ``ZeroDivisionError`` is not a ``CorrelationError``, so it
    escaped the whole loop, turning "one pair could not be computed" into "the
    entire correlation row failed" — reported as RISK_ENGINE_FAULT rather than
    as the designed refusal, and losing every other pair's answer with it.

    Not reachable from real market data: these are log returns of Decimal
    closes at four decimal places, and a return of 1e-146 cannot arise from
    them. It is fixed because the function's contract is that it returns a
    value in [-1, 1] or raises CorrelationError, and it did neither.
    """

    #: The exponent band where var_l stays above zero but the PRODUCT does not.
    #: Verified by sweep: below 1e-146 the product still fits; past 1e-161 the
    #: variance itself underflows and the existing zero-variance guard catches
    #: it, which is a different path and not this defect.
    UNDERFLOW_EXPONENTS = (146, 150, 155, 161)

    @staticmethod
    def _series(exponent: int) -> tuple[list[float], list[float]]:
        """One tiny non-zero value against an otherwise flat series — the shape
        Hypothesis landed on, reproduced deterministically."""
        xs = [10.0**-exponent] + [0.0] * (MIN_SESSIONS_FOR_CORRELATION - 1)
        return xs, [x * 2 + 0.01 for x in xs]

    @pytest.mark.parametrize("exponent", UNDERFLOW_EXPONENTS)
    def test_it_never_raises_zerodivisionerror(self, exponent: int) -> None:
        xs, ys = self._series(exponent)
        try:
            rho = pearson(xs, ys)
        except CorrelationError:
            return  # the honest refusal, and an acceptable answer here
        assert -1.0 <= rho <= 1.0

    @pytest.mark.parametrize("exponent", UNDERFLOW_EXPONENTS)
    def test_both_variances_really_are_positive(self, exponent: int) -> None:
        """The test above would pass trivially if these inputs were simply
        flat — the existing zero-variance guard would catch them and the
        underflow path would never be reached. This asserts the inputs get
        PAST that guard, which is what makes them a distinct defect."""
        xs, ys = self._series(exponent)
        n = len(xs)
        mean_l, mean_r = sum(xs) / n, sum(ys) / n
        var_l = sum((a - mean_l) ** 2 for a in xs)
        var_r = sum((b - mean_r) ** 2 for b in ys)
        assert var_l > 0
        assert var_r > 0
        assert var_l * var_r == 0.0, "this input no longer underflows; pick a smaller one"

    def test_an_uncomputable_pair_is_absent_rather_than_taking_the_row_down(self) -> None:
        """The consequence, at the level it is actually felt. One pathological
        series must cost that one pair, not every correlation in the row."""
        good = [Decimal("100") + Decimal(i) for i in range(40)]
        flat = [Decimal("100")] * 40
        result = correlations_against(
            "INFY",
            {
                "INFY": good,
                "TCS": [Decimal("100") + Decimal(i) * 2 for i in range(40)],
                "WIPRO": flat,
            },
            against=["TCS", "WIPRO"],
        )
        assert "TCS" in result, "a computable pair was lost"
        assert "WIPRO" not in result, "an uncomputable pair must be absent, not zero"

    def test_a_normal_correlation_is_unchanged_by_the_fix(self) -> None:
        """The control. sqrt(a)*sqrt(b) and sqrt(a*b) are the same number for
        ordinary inputs, and the fix must not have moved any real answer."""
        xs = [float(i % 7) - 3.0 for i in range(40)]
        ys = [2.0 * x + 1.0 for x in xs]
        assert pearson(xs, ys) == pytest.approx(1.0, abs=1e-12)
        assert pearson(xs, [-3.0 * x for x in xs]) == pytest.approx(-1.0, abs=1e-12)

    def test_a_real_correlation_survives_a_denormal_variance(self) -> None:
        """The test that distinguishes the FIX from merely guarding the symptom.

        Added because a mutation survived: reverting the denominator to
        ``sqrt(var_l * var_r)`` broke nothing, since the guard below it turned
        the underflow into a CorrelationError and every test here accepted a
        refusal as a valid outcome. Both spellings are safe; only one is
        *correct*.

        Here the true correlation is exactly 1.0 — ``ys = 2 * xs`` with no
        additive constant, so no floating-point noise disturbs the
        relationship — while both variances are denormal and their product is
        not representable. Refusing this pair would discard an answer that
        genuinely exists, and would do it silently: the pair would simply be
        absent from the row and read downstream as "unknown".
        """
        xs = [(i + 1) * 1e-160 for i in range(MIN_SESSIONS_FOR_CORRELATION)]
        ys = [2.0 * x for x in xs]

        n = len(xs)
        mean_l, mean_r = sum(xs) / n, sum(ys) / n
        var_l = sum((a - mean_l) ** 2 for a in xs)
        var_r = sum((b - mean_r) ** 2 for b in ys)
        assert var_l > 0 and var_r > 0, "both variances must clear the zero guard"
        assert var_l * var_r == 0.0, "the product must underflow, or this proves nothing"

        rho = pearson(xs, ys)  # must NOT raise
        assert rho == pytest.approx(1.0, abs=1e-5)
