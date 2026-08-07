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
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algotrader.common.enums import Direction, Regime, StrategyOrigin, Timeframe

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

    def validate_params(self, given: dict[str, Any]) -> None:
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

            if spec.type in ("int", "float"):
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                    raise ValueError(f"primitive {self.name!r}: {spec.name!r} must be numeric")
                numeric = Decimal(str(value))
                if spec.minimum is not None and numeric < spec.minimum:
                    raise ValueError(
                        f"primitive {self.name!r}: {spec.name!r}={numeric} "
                        f"below minimum {spec.minimum}"
                    )
                if spec.maximum is not None and numeric > spec.maximum:
                    raise ValueError(
                        f"primitive {self.name!r}: {spec.name!r}={numeric} "
                        f"above maximum {spec.maximum}"
                    )
            elif spec.type == "bool" and not isinstance(value, bool):
                raise ValueError(f"primitive {self.name!r}: {spec.name!r} must be bool")
            elif spec.type == "enum":
                if spec.choices and str(value) not in spec.choices:
                    raise ValueError(
                        f"primitive {self.name!r}: {spec.name!r}={value!r} not in {spec.choices}"
                    )


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

    primitive: str
    params: dict[str, Any] = Field(default_factory=dict)

    def validate_against(self, registry: PrimitiveRegistry) -> None:
        registry.get(self.primitive).validate_params(self.params)


class ConditionGroup(BaseModel):
    """Boolean composition of conditions.

    Deliberately limited to all_of / any_of / none_of.  A richer expression
    language would be more flexible and much harder to reason about or bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    all_of: list[Condition] = Field(default_factory=list)
    any_of: list[Condition] = Field(default_factory=list)
    none_of: list[Condition] = Field(default_factory=list)

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

        stop_spec = registry.get(self.stop.primitive)
        if stop_spec.category != "exit":
            raise ValueError(f"stop must be an exit primitive, got {self.stop.primitive!r}")
        if self.time.primitive != "squareoff_deadline":
            raise ValueError(
                "time exit must be 'squareoff_deadline' — every intraday position "
                "must close before the broker's per-stock auto square-off"
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
    min_price: Decimal = Decimal("100")
    sectors: list[str] | None = None


class StrategyConstraints(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_entries_per_day: int = Field(default=1, ge=1, le=10)
    entry_window: tuple[str, str] | None = None
    min_bars_since_open: int = Field(default=0, ge=0)


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
    except ValueError as exc:
        raise CompilationError(f"strategy {doc.id!r}: {exc}") from exc
    return doc


def load_strategy_yaml(text: str) -> StrategyDocument:
    """Parse a strategy from YAML.

    Uses ``safe_load`` — arbitrary Python object construction via YAML tags is
    another code-execution path and is not available here.
    """
    import yaml

    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise CompilationError("strategy YAML must be a mapping at the top level")
    return StrategyDocument.model_validate(raw)
