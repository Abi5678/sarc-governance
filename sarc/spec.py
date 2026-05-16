"""
SARC Constraint Specification DSL.

Defines the first-class constraint objects that govern agentic AI behavior.
Each constraint specifies what to check, when to check it, and what to do
when it's violated — making governance a structural part of the agent loop
rather than an external afterthought.

Based on: arXiv:2605.07728 — Governance-by-Architecture for Agentic AI
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ConstraintSource(str, Enum):
    """Where the constraint originates — determines override precedence."""
    REGULATORY = "regulatory"      # Legal / compliance (highest precedence)
    ORGANIZATIONAL = "organizational"  # Corporate policy
    OPERATIONAL = "operational"    # Runtime / system-level
    USER = "user"                  # End-user preference (lowest precedence)


class ConstraintClass(str, Enum):
    """Hard vs soft constraint — determines enforcement strictness."""
    HARD = "hard"   # Must never be violated; blocked at pre-action gate
    SOFT = "soft"   # Allowable within operating windows; monitored/throttled


class VerificationPoint(str, Enum):
    """Where in the agent loop the constraint is evaluated."""
    PRE_ACTION = "pre_action"          # Before action execution
    ACTION_TIME = "action_time"        # During action execution
    POST_ACTION = "post_action"        # After action completion
    PERIODIC = "periodic"              # Background periodic check


class ResponseProtocol(str, Enum):
    """What happens when a constraint is violated."""
    BLOCK = "block"              # Prevent the action entirely
    THROTTLE = "throttle"        # Reduce action intensity/frequency
    ESCALATE = "escalate"        # Route to human oversight
    LOG_AND_CONTINUE = "log_and_continue"  # Record but allow
    ROLLBACK = "rollback"        # Undo the action if possible


# ---------------------------------------------------------------------------
# Predicate protocol
# ---------------------------------------------------------------------------

class Predicate(Protocol):
    """A callable that evaluates whether a constraint is satisfied.

    Returns (satisfied: bool, details: dict).
    """
    def __call__(self, context: dict[str, Any]) -> tuple[bool, dict[str, Any]]: ...


# Type alias for flexible predicate definitions
PredicateFn = Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]]


# ---------------------------------------------------------------------------
# Operating point — soft-constraint tolerance window
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperatingPoint:
    """Defines the acceptable operating window for a soft constraint.

    For example, a budget constraint might allow spending up to `threshold`
    per action, but cumulative spending should stay below `ceiling`. The
    `rate_limit` controls maximum action frequency.
    """
    threshold: float | None = None      # Per-action limit
    ceiling: float | None = None        # Cumulative limit
    rate_limit: float | None = None     # Max actions per second
    window_seconds: float = 60.0        # Sliding window for rate limiting
    tolerance_pct: float = 0.0          # Acceptable overshoot percentage


# ---------------------------------------------------------------------------
# Constraint specification
# ---------------------------------------------------------------------------

@dataclass
class ConstraintSpec:
    """A first-class governance constraint specification.

    In SARC, constraints are specification objects on equal footing with
    state, action space, and reward. Each spec declares:
      - What to check (predicate)
      - When to check it (verification_point)
      - What class it belongs to (hard/soft)
      - Where it comes from (source — determines precedence)
      - What to do on violation (response_protocol)
      - How much tolerance is allowed (operating_point)
    """
    name: str
    description: str
    source: ConstraintSource
    constraint_class: ConstraintClass
    predicate: PredicateFn
    verification_point: VerificationPoint = VerificationPoint.PRE_ACTION
    response_protocol: ResponseProtocol = ResponseProtocol.BLOCK
    operating_point: OperatingPoint | None = None
    tags: list[str] = field(default_factory=list)

    # Metadata
    version: str = "1.0.0"
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.constraint_class == ConstraintClass.HARD:
            if self.response_protocol not in (
                ResponseProtocol.BLOCK,
                ResponseProtocol.ESCALATE,
                ResponseProtocol.ROLLBACK,
            ):
                raise ValueError(
                    f"Hard constraint '{self.name}' must use BLOCK, ESCALATE, "
                    f"or ROLLBACK response protocol, got {self.response_protocol.value}"
                )
        if self.constraint_class == ConstraintClass.SOFT and self.operating_point is None:
            # Soft constraints default to a permissive operating point
            self.operating_point = OperatingPoint()

    def evaluate(self, context: dict[str, Any]) -> ConstraintResult:
        """Evaluate this constraint against the given context."""
        satisfied, details = self.predicate(context)
        return ConstraintResult(
            constraint_name=self.name,
            constraint_class=self.constraint_class,
            satisfied=satisfied,
            details=details,
            timestamp=time.time(),
            source=self.source,
            response_protocol=self.response_protocol,
        )

    def fingerprint(self) -> str:
        """Deterministic hash for reproducibility and audit."""
        payload = json.dumps({
            "name": self.name,
            "description": self.description,
            "source": self.source.value,
            "class": self.constraint_class.value,
            "vp": self.verification_point.value,
            "rp": self.response_protocol.value,
            "version": self.version,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Constraint result
# ---------------------------------------------------------------------------

@dataclass
class ConstraintResult:
    """Outcome of evaluating a single constraint."""
    constraint_name: str
    constraint_class: ConstraintClass
    satisfied: bool
    details: dict[str, Any]
    timestamp: float
    source: ConstraintSource
    response_protocol: ResponseProtocol


# ---------------------------------------------------------------------------
# Constraint registry
# ---------------------------------------------------------------------------

class ConstraintRegistry:
    """Central registry for all active constraints.

    Supports lookup by name, source, class, or verification point.
    This is the single source of truth for which constraints govern
    an agent's behavior at any given moment.
    """

    def __init__(self) -> None:
        self._constraints: dict[str, ConstraintSpec] = {}

    def register(self, spec: ConstraintSpec) -> None:
        if spec.name in self._constraints:
            raise KeyError(f"Constraint '{spec.name}' already registered")
        self._constraints[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._constraints.pop(name, None)

    def get(self, name: str) -> ConstraintSpec | None:
        return self._constraints.get(name)

    def all_constraints(self) -> list[ConstraintSpec]:
        return list(self._constraints.values())

    def by_source(self, source: ConstraintSource) -> list[ConstraintSpec]:
        return [c for c in self._constraints.values() if c.source == source]

    def by_class(self, cls: ConstraintClass) -> list[ConstraintSpec]:
        return [c for c in self._constraints.values() if c.constraint_class == cls]

    def by_verification_point(self, vp: VerificationPoint) -> list[ConstraintSpec]:
        return [c for c in self._constraints.values() if c.verification_point == vp]

    def evaluate_all(
        self,
        context: dict[str, Any],
        vp: VerificationPoint | None = None,
    ) -> list[ConstraintResult]:
        """Evaluate all (or VP-filtered) constraints against context."""
        constraints = (
            self.by_verification_point(vp) if vp else self.all_constraints()
        )
        return [c.evaluate(context) for c in constraints]

    def __len__(self) -> int:
        return len(self._constraints)

    def __contains__(self, name: str) -> bool:
        return name in self._constraints
