"""Strategy DSL — strategies are declarative data, never code.

This module implements the governing safety decision from STRATEGY_ENGINE.md
§1.3:

    The AI never writes executable code.  It composes strategies from a
    vetted primitive library using a declarative DSL.

That is the exact parallel of the platform's existing rule that the LLM never
computes position size: the AI expresses intent in a constrained vocabulary,
and deterministic code interprets it.

Consequences that fall out of this choice:

* There is no ``eval``, ``exec``, or dynamic import anywhere in the strategy
  path — arbitrary code execution is not mitigated, it is *impossible*.
* A prompt injection that fully compromises strategy generation can at worst
  produce a bad-but-valid strategy, which still faces the whole validation
  gauntlet.
* Strategies are diffable, reviewable, and self-documenting — which also
  aligns with SEBI's preference for transparent, rule-based algorithms over
  black boxes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algotrader.common.enums import Direction, Regime, StrategyOrigin, Timeframe

#: Size bounds on a strategy document (27 Oct 2026). The 12-condition cap was
#: checked only AFTER parsing, so a document with a million conditions was
#: built in memory first; a strategy is untrusted input from the AI, and an
#: unbounded collection is a denial of service before any rule runs.
MAX_CONDITIONS_PER_GROUP = 12
MAX_PARAMS_PER_CONDITION = 16
MAX_STRING_PARAM_LENGTH = 64
MAX_SECTORS = 32
MAX_STRATEGY_YAML_BYTES = 64 * 1024

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


# ---------------------------------------------------------------------------
# Primitive registry
# ---------------------------------------------------------------------------


class ParamSpec(BaseModel):
    """Declared bounds for one primitive parameter."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["int", "float", "str", "bool", "enum"]
    required: bool = True
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    choices: list[str] | None = None
    default: Any = None


class PrimitiveSpec(BaseModel):
    """A vetted, hand-written, unit-tested building block.

    Primitives are the ONLY vocabulary available to a strategy.  Adding one
    is a human code change that goes through review — the AI cannot define
    new primitives, only reference existing ones.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    category: Literal[
        "price",
        "trend",
        "momentum",
        "volatility",
        "volume",
        "multiframe",
        "context",
        "news",
        "time",
        "exit",
    ]
    description: str
    params: list[ParamSpec] = Field(default_factory=list)

    #: Exit primitives that a strategy may not omit.  A strategy literally
    #: cannot express "no stop loss" because the DSL has no way to say it.
    is_mandatory_exit: bool = False

    #: Which exit SLOT an exit primitive may fill. "Is an exit primitive" was
    #: the whole test for ``exit.stop`` until 27 Oct 2026, and
    #: ``r_multiple_target``, ``trail_after_r`` and ``squareoff_deadline`` are
    #: all exit primitives - so ``stop: r_multiple_target`` compiled, and the
    #: sentence above was false: a strategy COULD express "no stop loss".
    exit_role: Literal["stop", "time", "target", "trail"] | None = None

    @model_validator(mode="after")
    def _role_matches_category(self) -> Self:
        if (self.category == "exit") != (self.exit_role is not None):
            raise ValueError(
                f"primitive {self.name!r}: an exit primitive must declare its exit_role, "
                f"and only an exit primitive may (category={self.category!r}, "
                f"exit_role={self.exit_role!r})"
            )
        return self

    def validate_params(self, given: dict[str, Any]) -> None:
        """Every parameter declared, typed, finite and in range.

        Hardened 27 Oct 2026 against input the AI controls:

        * ``int`` takes an int and nothing else. A float was accepted and then
          truncated at runtime (14.7 ran as 14) while the content hash kept
          14.7 - so two strategies that behave identically counted as two
          trials, inflating the multiple-testing count the overfitting
          control depends on.
        * a number must be FINITE. NaN passed any parameter with no declared
          bound, and against a bound raised ``InvalidOperation`` - not a
          ``ValueError`` - which ``compile_strategy`` does not catch.
        * ``str`` was not checked at all; ``enum`` accepted any type whose
          ``str()`` happened to match.
        """
        specs = {p.name: p for p in self.params}

        unknown = set(given) - set(specs)
        if unknown:
            raise ValueError(f"primitive {self.name!r}: unknown parameter(s) {sorted(unknown)}")

        for spec in self.params:
            if spec.name not in given:
                if spec.required and spec.default is None:
                    raise ValueError(
                        f"primitive {self.name!r}: missing required parameter {spec.name!r}"
                    )
                continue

            value = given[spec.name]
            where = f"primitive {self.name!r}: {spec.name!r}"

            if spec.type in ("int", "float"):
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                    raise ValueError(f"{where} must be numeric")
                if spec.type == "int" and not isinstance(value, int):
                    raise ValueError(
                        f"{where} must be a whole number, got {value!r}. A fraction would "
                        f"be truncated when the strategy runs while the content hash kept "
                        f"it, so two identical strategies would count as two trials."
                    )
                numeric = Decimal(str(value))
                if not numeric.is_finite():
                    raise ValueError(f"{where} must be a finite number, got {value!r}")
                if spec.minimum is not None and numeric < spec.minimum:
                    raise ValueError(f"{where}={numeric} below minimum {spec.minimum}")
                if spec.maximum is not None and numeric > spec.maximum:
                    raise ValueError(f"{where}={numeric} above maximum {spec.maximum}")
            elif spec.type == "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"{where} must be bool")
            elif spec.type == "str":
                if not isinstance(value, str):
                    raise ValueError(f"{where} must be a string")
                if not value or len(value) > MAX_STRING_PARAM_LENGTH:
                    raise ValueError(
                        f"{where} must be 1..{MAX_STRING_PARAM_LENGTH} characters, got {len(value)}"
                    )
            elif spec.type == "enum":
                if not spec.choices:
                    raise ValueError(f"{where} is an enum with no declared choices")
                if not isinstance(value, str) or value not in spec.choices:
                    raise ValueError(f"{where}={value!r} not in {spec.choices}")


class PrimitiveRegistry:
    """The closed set of primitives a strategy may reference."""

    def __init__(self) -> None:
        self._specs: dict[str, PrimitiveSpec] = {}

    def register(self, spec: PrimitiveSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"primitive {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> PrimitiveSpec:
        if name not in self._specs:
            raise ValueError(
                f"unknown primitive {name!r}. Strategies may only reference "
                f"primitives in the vetted registry; new primitives require a "
                f"reviewed code change."
            )
        return self._specs[name]

    def names(self) -> list[str]:
        return sorted(self._specs)

    def by_category(self, category: str) -> list[PrimitiveSpec]:
        return [s for s in self._specs.values() if s.category == category]


#: Process-wide registry, populated by ``algotrader.strategy.primitives``.
REGISTRY = PrimitiveRegistry()


# ---------------------------------------------------------------------------
# Strategy document
# ---------------------------------------------------------------------------


class Condition(BaseModel):
    """One primitive invocation with its parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primitive: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict, max_length=MAX_PARAMS_PER_CONDITION)

    def validate_against(self, registry: PrimitiveRegistry) -> None:
        registry.get(self.primitive).validate_params(self.params)


class ConditionGroup(BaseModel):
    """Boolean composition of conditions.

    Deliberately limited to all_of / any_of / none_of.  A richer expression
    language would be more flexible and much harder to reason about or bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    all_of: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS_PER_GROUP)
    any_of: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS_PER_GROUP)
    none_of: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS_PER_GROUP)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not (self.all_of or self.any_of or self.none_of):
            raise ValueError("a condition group must contain at least one condition")
        return self

    def all_conditions(self) -> list[Condition]:
        return [*self.all_of, *self.any_of, *self.none_of]

    def validate_against(self, registry: PrimitiveRegistry) -> None:
        for condition in self.all_conditions():
            condition.validate_against(registry)


class ExitRules(BaseModel):
    """Exit configuration.

    ``stop`` and ``time`` are REQUIRED.  This is where the "every position
    has a protective stop" and "every intraday position exits before the
    broker's deadline" invariants are enforced at the type level: a strategy
    without them will not parse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop: Condition
    time: Condition
    target: Condition | None = None
    trail: Condition | None = None

    def validate_against(self, registry: PrimitiveRegistry) -> None:
        for cond in (self.stop, self.time, self.target, self.trail):
            if cond is not None:
                cond.validate_against(registry)

        if self.time.primitive != "squareoff_deadline":
            raise ValueError(
                "time exit must be 'squareoff_deadline' — every intraday position "
                "must close before the broker's per-stock auto square-off"
            )
        slots: tuple[tuple[str, Condition | None], ...] = (
            ("stop", self.stop),
            ("time", self.time),
            ("target", self.target),
            ("trail", self.trail),
        )
        for slot, cond in slots:
            if cond is None:
                continue
            role = registry.get(cond.primitive).exit_role
            if role != slot:
                raise ValueError(
                    f"exit.{slot} must be a {slot} primitive, got {cond.primitive!r} "
                    f"(a {role or 'non-exit'} primitive). An exit primitive is not "
                    f"interchangeable with another: a target in the stop slot is a "
                    f"strategy with no stop loss."
                )


class Hypothesis(BaseModel):
    """The economic rationale, frozen BEFORE any backtest runs.

    This ordering is the strongest cheap control against data mining
    (STRATEGY_ENGINE.md §7.2).  A strategy whose author can articulate why it
    should work, and whose realized failures match its predicted failures, is
    meaningfully different from one that merely scored well on history.

    Generic boilerplate fails validation — the minimum lengths are there to
    make a non-answer detectable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism: str = Field(min_length=80, max_length=2000)
    why_it_should_persist: str = Field(min_length=60, max_length=2000)
    expected_failure_mode: str = Field(min_length=40, max_length=2000)

    @field_validator("mechanism", "why_it_should_persist", "expected_failure_mode")
    @classmethod
    def _not_boilerplate(cls, v: str) -> str:
        lowered = v.lower().strip()
        vacuous = (
            "n/a",
            "none",
            "tbd",
            "unknown",
            "not applicable",
            "the strategy works",
            "it makes money",
            "backtest shows",
        )
        if lowered in vacuous or any(lowered.startswith(p) for p in vacuous):
            raise ValueError(
                "hypothesis fields must state a real economic mechanism. A "
                "strategy that cannot be explained is data mining."
            )
        return v


class Applicability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regimes: list[Regime] = Field(min_length=1)
    timeframe: Timeframe
    min_price: Decimal = Field(default=Decimal("100"), ge=0, allow_inf_nan=False)
    sectors: list[str] | None = Field(default=None, min_length=1, max_length=MAX_SECTORS)

    @field_validator("sectors")
    @classmethod
    def _named_sectors(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(not s.strip() or len(s) > 64 for s in v):
            raise ValueError("every sector must be a non-blank name of at most 64 characters")
        return v


class StrategyConstraints(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_entries_per_day: int = Field(default=1, ge=1, le=10)
    #: IST wall-clock window, start inclusive and end exclusive, "HH:MM".
    entry_window: tuple[str, str] | None = None
    min_bars_since_open: int = Field(default=0, ge=0, le=400)

    @field_validator("entry_window")
    @classmethod
    def _a_real_window(cls, v: tuple[str, str] | None) -> tuple[str, str] | None:
        """Parsed here so a window that can never match fails at compile
        time - the runtime reads it on every evaluation."""
        if v is None:
            return v
        start, end = v
        for text in (start, end):
            if not isinstance(text, str) or _HHMM.fullmatch(text) is None:
                raise ValueError(f"entry_window bounds must be 'HH:MM', got {text!r}")
        if start >= end:
            raise ValueError(f"entry_window {start}-{end} is empty or inverted")
        return v

    def window_times(self) -> tuple[dt_time, dt_time] | None:
        if self.entry_window is None:
            return None
        start, end = self.entry_window
        return dt_time.fromisoformat(start), dt_time.fromisoformat(end)


class StrategyDocument(BaseModel):
    """A complete strategy definition.

    This is what a user writes, what the AI proposes, and what the compiler
    turns into a runnable strategy.  It is data end to end.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[a-z0-9_]+$")]
    name: str = Field(min_length=3, max_length=128)
    version: int = Field(default=1, ge=1)
    parent_id: str | None = None
    origin: StrategyOrigin
    created_at: datetime
    created_by: str = Field(max_length=64)

    hypothesis: Hypothesis
    applicability: Applicability
    direction: Direction

    entry: ConditionGroup
    exit: ExitRules
    constraints: StrategyConstraints = Field(default_factory=StrategyConstraints)

    @field_validator("created_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return v

    def content_hash(self) -> str:
        """Stable hash of the *behavioural* content.

        Excludes metadata (name, timestamps, authorship) so that genuinely
        identical strategies deduplicate in the trial registry while distinct
        variants each count as their own trial.
        """
        payload = {
            "direction": self.direction.value,
            "applicability": self.applicability.model_dump(mode="json"),
            "entry": self.entry.model_dump(mode="json"),
            "exit": self.exit.model_dump(mode="json"),
            "constraints": self.constraints.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def validate_against(self, registry: PrimitiveRegistry = REGISTRY) -> None:
        """Structural validation against the primitive registry.

        Raises ValueError with a specific message on the first problem.
        """
        self.entry.validate_against(registry)
        self.exit.validate_against(registry)

        for condition in self.entry.all_conditions():
            if registry.get(condition.primitive).category == "exit":
                raise ValueError(
                    f"entry condition {condition.primitive!r} is an exit primitive. Its "
                    f"evaluator returns a price, not a verdict, and has no place in an "
                    f"entry group."
                )

        if len(self.entry.all_conditions()) > 12:
            raise ValueError(
                "strategy has more than 12 entry conditions; that many degrees "
                "of freedom is a strong overfitting signal"
            )


class CompilationError(ValueError):
    """Raised when a strategy document cannot be compiled."""


def compile_strategy(
    doc: StrategyDocument, registry: PrimitiveRegistry = REGISTRY
) -> StrategyDocument:
    """Validate a document and return it ready for execution.

    Returns the document rather than a closure so the caller keeps full
    provenance.  The runtime evaluator walks the validated tree; there is no
    code generation step at any point.
    """
    try:
        doc.validate_against(registry)
    except (ValueError, ArithmeticError) as exc:
        raise CompilationError(f"strategy {doc.id!r}: {exc}") from exc
    return doc


def load_strategy_yaml(text: str) -> StrategyDocument:
    """Parse a strategy from YAML.

    Uses ``safe_load`` — arbitrary Python object construction via YAML tags is
    another code-execution path and is not available here.
    """
    import yaml

    # Bounded before parsing, and no anchors or aliases: ``safe_load`` stops
    # code execution but not a YAML bomb, where a few lines of nested aliases
    # expand into billions of nodes as soon as anything walks the result
    # (validation does). A strategy document has no use for either.
    if len(text.encode("utf-8")) > MAX_STRATEGY_YAML_BYTES:
        raise CompilationError(
            f"strategy YAML is larger than {MAX_STRATEGY_YAML_BYTES} bytes; refusing to parse"
        )

    class _NoAliasLoader(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):  # type: ignore[no-untyped-call]
                raise CompilationError("strategy YAML may not use aliases (*name)")
            event = self.peek_event()  # type: ignore[no-untyped-call]
            if getattr(event, "anchor", None):
                raise CompilationError("strategy YAML may not use anchors (&name)")
            return super().compose_node(parent, index)

    # A SafeLoader subclass: same constructors as safe_load, plus the refusals
    # above. Backed by an executed check, not this comment -
    # tests/unit/test_strategy_dsl.py::TestNoCodeExecution proves a python tag
    # is refused by the constructor and that os.system is never reached.
    raw = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506  # nosec B506
    if not isinstance(raw, dict):
        raise CompilationError("strategy YAML must be a mapping at the top level")
    return StrategyDocument.model_validate(raw)
