"""
SARC Enforcement Sites — The Four Gates.

This module implements the four enforcement sites from the SARC architecture:

1. PreActionGate   — Blocks actions violating hard constraints before execution
2. ActionMonitor   — Observes ongoing actions for soft-constraint drift
3. PostActionAuditor — Audits completed actions for compliance
4. EscalationRouter — Routes violations to human oversight or remediation

Key insight: constraints are evaluated at structurally guaranteed points
in the agent loop, not bolted on as post-hoc filters.

Based on: arXiv:2605.07728
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Protocol
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

from .spec import (
    ConstraintClass,
    ConstraintRegistry,
    ConstraintResult,
    ConstraintSpec,
    OperatingPoint,
    ResponseProtocol,
    VerificationPoint,
)
from .trace import TraceTree


# ---------------------------------------------------------------------------
# Enforcement decision
# ---------------------------------------------------------------------------

class EnforcementDecision(str, Enum):
    """Outcome of an enforcement check."""
    ALLOW = "allow"
    BLOCK = "block"
    THROTTLE = "throttle"
    ESCALATE = "escalate"
    ROLLBACK = "rollback"


@dataclass
class EnforcementResult:
    """Result from any enforcement site."""
    decision: EnforcementDecision
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    reason: str = ""
    throttled_params: dict[str, Any] | None = None
    escalation_id: str | None = None


# ---------------------------------------------------------------------------
# Escalation handler protocol
# ---------------------------------------------------------------------------

class EscalationHandler(Protocol):
    """Handler for escalated violations."""
    def handle(
        self,
        violation: ConstraintResult,
        context: dict[str, Any],
        trace: TraceTree | None = None,
    ) -> EnforcementDecision: ...


# Type alias
EscalationHandlerFn = Callable[
    [ConstraintResult, dict[str, Any], TraceTree | None],
    EnforcementDecision,
]


# ---------------------------------------------------------------------------
# 1. Pre-Action Gate
# ---------------------------------------------------------------------------

class PreActionGate:
    """First enforcement site: blocks actions that violate hard constraints.

    This is the primary safety gate. Every proposed action passes through
    here before execution. Hard constraints are evaluated exactly; if any
    hard constraint is violated, the action is blocked regardless of other
    considerations.

    The paper shows this achieves zero hard-constraint violations under
    exact predicates.
    """

    def __init__(self, registry: ConstraintRegistry) -> None:
        self.registry = registry

    def check(
        self,
        action: str,
        action_params: dict[str, Any],
        context: dict[str, Any],
    ) -> EnforcementResult:
        """Evaluate all pre-action constraints for a proposed action.

        Hard constraints: any violation → BLOCK
        Soft constraints: violation → THROTTLE or LOG_AND_CONTINUE
        """
        # Merge action info into evaluation context
        eval_context = {
            **context,
            "_action": action,
            "_action_params": action_params,
            "_check_time": time.time(),
        }

        constraints = self.registry.by_verification_point(
            VerificationPoint.PRE_ACTION
        )

        results: list[ConstraintResult] = []
        hard_violation: ConstraintResult | None = None
        soft_violations: list[ConstraintResult] = []

        for spec in constraints:
            result = spec.evaluate(eval_context)
            results.append(result)

            if not result.satisfied:
                if result.constraint_class == ConstraintClass.HARD:
                    hard_violation = result
                    break  # Short-circuit on hard violation
                else:
                    soft_violations.append(result)

        # Hard violation → immediate block
        if hard_violation is not None:
            return EnforcementResult(
                decision=EnforcementDecision.BLOCK,
                constraint_results=results,
                reason=f"Hard constraint '{hard_violation.constraint_name}' violated: {hard_violation.details}",
            )

        # Soft violations → throttle or allow with logging
        if soft_violations:
            throttled = self._apply_throttling(
                soft_violations, action_params, eval_context
            )
            return EnforcementResult(
                decision=EnforcementDecision.THROTTLE,
                constraint_results=results,
                reason=f"Soft constraints violated: {[v.constraint_name for v in soft_violations]}",
                throttled_params=throttled,
            )

        return EnforcementResult(
            decision=EnforcementDecision.ALLOW,
            constraint_results=results,
        )

    def _apply_throttling(
        self,
        violations: list[ConstraintResult],
        action_params: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply soft-constraint throttling to action parameters.

        For PAA (Per-Action Amount) throttling: reduce the amount to the
        operating point threshold.
        """
        throttled = dict(action_params)
        for v in violations:
            # Look up the original constraint for its operating point
            spec = self.registry.get(v.constraint_name)
            if spec and spec.operating_point and spec.operating_point.threshold is not None:
                # Throttle amount fields down to threshold
                for key in ("amount", "value", "cost", "budget"):
                    if key in throttled:
                        current = throttled[key]
                        if isinstance(current, (int, float)):
                            throttled[key] = min(current, spec.operating_point.threshold)
        return throttled


# ---------------------------------------------------------------------------
# 2. Action-Time Monitor
# ---------------------------------------------------------------------------

class ActionMonitor:
    """Second enforcement site: monitors ongoing actions for soft-constraint drift.

    While the pre-action gate catches upfront violations, the action monitor
    tracks cumulative metrics during execution. This catches:
      - Cumulative budget overages (ceiling violations)
      - Rate limit breaches
      - Gradual constraint drift

    The monitor maintains state across multiple checks within a session.
    """

    def __init__(self, registry: ConstraintRegistry) -> None:
        self.registry = registry
        self._cumulative: dict[str, float] = {}
        self._action_timestamps: dict[str, list[float]] = {}

    def check(
        self,
        action: str,
        action_params: dict[str, Any],
        context: dict[str, Any],
    ) -> EnforcementResult:
        """Monitor ongoing action against action-time constraints."""
        eval_context = {
            **context,
            "_action": action,
            "_action_params": action_params,
            "_check_time": time.time(),
            "_cumulative": dict(self._cumulative),
        }

        constraints = self.registry.by_verification_point(
            VerificationPoint.ACTION_TIME
        )

        results: list[ConstraintResult] = []
        soft_violations: list[ConstraintResult] = []

        for spec in constraints:
            # Inject cumulative state into context for predicate evaluation
            result = spec.evaluate(eval_context)
            results.append(result)

            if not result.satisfied:
                soft_violations.append(result)

        # Update cumulative tracking
        self._update_tracking(action, action_params)

        if soft_violations:
            # Determine if any require escalation
            for v in soft_violations:
                spec = self.registry.get(v.constraint_name)
                if spec and spec.response_protocol == ResponseProtocol.ESCALATE:
                    return EnforcementResult(
                        decision=EnforcementDecision.ESCALATE,
                        constraint_results=results,
                        reason=f"Soft constraint escalation: {v.constraint_name}",
                    )

            return EnforcementResult(
                decision=EnforcementDecision.THROTTLE,
                constraint_results=results,
                reason=f"Soft constraint drift detected: {[v.constraint_name for v in soft_violations]}",
            )

        return EnforcementResult(
            decision=EnforcementDecision.ALLOW,
            constraint_results=results,
        )

    def _update_tracking(self, action: str, params: dict[str, Any]) -> None:
        """Update cumulative metrics and rate-limit tracking."""
        now = time.time()
        for key in ("amount", "value", "cost", "budget"):
            if key in params and isinstance(params[key], (int, float)):
                self._cumulative[key] = self._cumulative.get(key, 0.0) + params[key]

        # Rate limit tracking
        action_key = action
        if action_key not in self._action_timestamps:
            self._action_timestamps[action_key] = []
        self._action_timestamps[action_key].append(now)

    def get_cumulative(self, key: str) -> float:
        return self._cumulative.get(key, 0.0)

    def reset(self) -> None:
        """Reset monitoring state (e.g., at start of new session)."""
        self._cumulative.clear()
        self._action_timestamps.clear()


# ---------------------------------------------------------------------------
# 3. Post-Action Auditor
# ---------------------------------------------------------------------------

class PostActionAuditor:
    """Third enforcement site: audits completed actions for compliance.

    Post-action auditing serves two purposes:
      1. Catch violations that weren't visible at pre-action time
         (e.g., side effects, emergent outcomes)
      2. Build an audit trail for compliance reporting

    For hard constraints, a post-action violation triggers rollback if
    possible, or escalation to human review.
    """

    def __init__(self, registry: ConstraintRegistry) -> None:
        self.registry = registry
        self._audit_log: list[dict[str, Any]] = []

    def audit(
        self,
        action: str,
        action_params: dict[str, Any],
        outcome: dict[str, Any],
        context: dict[str, Any],
    ) -> EnforcementResult:
        """Audit a completed action against post-action constraints."""
        eval_context = {
            **context,
            "_action": action,
            "_action_params": action_params,
            "_outcome": outcome,
            "_audit_time": time.time(),
        }

        constraints = self.registry.by_verification_point(
            VerificationPoint.POST_ACTION
        )

        results: list[ConstraintResult] = []
        hard_violations: list[ConstraintResult] = []
        soft_violations: list[ConstraintResult] = []

        for spec in constraints:
            result = spec.evaluate(eval_context)
            results.append(result)

            if not result.satisfied:
                if result.constraint_class == ConstraintClass.HARD:
                    hard_violations.append(result)
                else:
                    soft_violations.append(result)

        # Log the audit
        audit_entry = {
            "action": action,
            "params": action_params,
            "outcome": outcome,
            "results": [
                {
                    "constraint": r.constraint_name,
                    "class": r.constraint_class.value,
                    "satisfied": r.satisfied,
                    "details": r.details,
                }
                for r in results
            ],
            "timestamp": time.time(),
        }
        self._audit_log.append(audit_entry)

        # Hard violation → rollback or escalate
        if hard_violations:
            can_rollback = outcome.get("_rollback_possible", False)
            if can_rollback:
                return EnforcementResult(
                    decision=EnforcementDecision.ROLLBACK,
                    constraint_results=results,
                    reason=f"Post-action hard constraint violation: {[v.constraint_name for v in hard_violations]}",
                )
            return EnforcementResult(
                decision=EnforcementDecision.ESCALATE,
                constraint_results=results,
                reason=f"Post-action hard constraint violation (no rollback): {[v.constraint_name for v in hard_violations]}",
            )

        # Soft violation → log or escalate
        if soft_violations:
            for v in soft_violations:
                if v.response_protocol == ResponseProtocol.ESCALATE:
                    return EnforcementResult(
                        decision=EnforcementDecision.ESCALATE,
                        constraint_results=results,
                        reason=f"Post-action soft constraint escalation: {v.constraint_name}",
                    )
            return EnforcementResult(
                decision=EnforcementDecision.ALLOW,  # Already executed
                constraint_results=results,
                reason=f"Post-action soft constraint logged: {[v.constraint_name for v in soft_violations]}",
            )

        return EnforcementResult(
            decision=EnforcementDecision.ALLOW,
            constraint_results=results,
        )

    def get_audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit_log)

    def clear_log(self) -> None:
        self._audit_log.clear()


# ---------------------------------------------------------------------------
# 4. Escalation Router
# ---------------------------------------------------------------------------

@dataclass
class EscalationRecord:
    """Record of an escalated violation."""
    escalation_id: str = field(default_factory=lambda: f"esc-{uuid.uuid4().hex[:8]}")
    constraint_name: str = ""
    agent_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    decision: EnforcementDecision | None = None
    human_response: str | None = None
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False


import uuid


class EscalationRouter:
    """Fourth enforcement site: routes violations to human oversight or remediation.

    The escalation router handles violations that require human judgment:
      - Hard constraint violations in ambiguous contexts
      - Soft constraint patterns that exceed operational norms
      - Cross-agent conflicts that need arbitration

    It maintains a queue of pending escalations and supports both
    synchronous (immediate) and asynchronous (deferred) resolution.
    """

    def __init__(
        self,
        handler: EscalationHandlerFn | None = None,
    ) -> None:
        self._handler = handler or self._default_handler
        self._escalations: list[EscalationRecord] = []
        self._auto_responses: dict[str, EnforcementDecision] = {}

    def route(
        self,
        result: ConstraintResult,
        context: dict[str, Any],
        trace: TraceTree | None = None,
    ) -> EnforcementResult:
        """Route a violation through the escalation process."""
        record = EscalationRecord(
            constraint_name=result.constraint_name,
            agent_id=context.get("_agent_id", "unknown"),
            context=context,
        )
        self._escalations.append(record)

        # Check for auto-response rules
        auto = self._auto_responses.get(result.constraint_name)
        if auto is not None:
            record.decision = auto
            record.resolved = True
            return EnforcementResult(
                decision=auto,
                reason=f"Auto-resolved escalation for '{result.constraint_name}'",
                escalation_id=record.escalation_id,
            )

        # Call the handler
        decision = self._handler(result, context, trace)
        record.decision = decision
        record.resolved = True

        return EnforcementResult(
            decision=decision,
            constraint_results=[result],
            reason=f"Escalated '{result.constraint_name}' → {decision.value}",
            escalation_id=record.escalation_id,
        )

    def set_auto_response(
        self, constraint_name: str, decision: EnforcementDecision
    ) -> None:
        """Configure automatic response for a specific constraint."""
        self._auto_responses[constraint_name] = decision

    def get_pending_escalations(self) -> list[EscalationRecord]:
        return [e for e in self._escalations if not e.resolved]

    def get_all_escalations(self) -> list[EscalationRecord]:
        return list(self._escalations)

    def resolve(
        self,
        escalation_id: str,
        decision: EnforcementDecision,
        human_response: str = "",
    ) -> None:
        """Manually resolve an escalation (e.g., by a human reviewer)."""
        for esc in self._escalations:
            if esc.escalation_id == escalation_id:
                esc.decision = decision
                esc.human_response = human_response
                esc.resolved = True
                break

    @staticmethod
    def _default_handler(
        result: ConstraintResult,
        context: dict[str, Any],
        trace: TraceTree | None,
    ) -> EnforcementDecision:
        """Default: block hard violations, allow soft ones with logging."""
        if result.constraint_class == ConstraintClass.HARD:
            return EnforcementDecision.BLOCK
        return EnforcementDecision.ALLOW
