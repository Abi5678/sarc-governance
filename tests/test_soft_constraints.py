"""
Tests for SARC soft constraint enforcement and throttling.
"""

import pytest
from sarc.spec import (
    ConstraintClass,
    ConstraintRegistry,
    ConstraintSource,
    ConstraintSpec,
    OperatingPoint,
    ResponseProtocol,
    VerificationPoint,
)
from sarc.enforcement import EnforcementDecision, PreActionGate, ActionMonitor
from sarc.agent_loop import AgentAction, GovernedAgentLoop


def make_soft_threshold_constraint(threshold: float = 300.0) -> ConstraintSpec:
    return ConstraintSpec(
        name="soft_spend_threshold",
        description=f"Preferred spending ≤ ${threshold}",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx, t=threshold: (
            ctx.get("_action_params", {}).get("amount", 0) <= t,
            {"amount": ctx.get("_action_params", {}).get("amount", 0), "threshold": t},
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.THROTTLE,
        operating_point=OperatingPoint(threshold=threshold),
    )


class TestSoftConstraintThrottling:
    def test_soft_violation_throttles(self):
        registry = ConstraintRegistry()
        registry.register(make_soft_threshold_constraint(300))
        gate = PreActionGate(registry)

        result = gate.check("purchase", {"amount": 500}, {})
        assert result.decision == EnforcementDecision.THROTTLE

    def test_throttled_amount_reduced_to_threshold(self):
        registry = ConstraintRegistry()
        registry.register(make_soft_threshold_constraint(300))
        gate = PreActionGate(registry)

        result = gate.check("purchase", {"amount": 500}, {})
        assert result.throttled_params is not None
        assert result.throttled_params["amount"] == 300

    def test_within_threshold_not_throttled(self):
        registry = ConstraintRegistry()
        registry.register(make_soft_threshold_constraint(300))
        gate = PreActionGate(registry)

        result = gate.check("purchase", {"amount": 200}, {})
        assert result.decision == EnforcementDecision.ALLOW

    def test_throttle_reduces_but_doesnt_block(self):
        """Soft constraint violations should reduce spending, not block."""
        registry = ConstraintRegistry()
        registry.register(make_soft_threshold_constraint(300))
        loop = GovernedAgentLoop(registry=registry, agent_id="test")

        result = loop.step(AgentAction(action="purchase", params={"amount": 500}))
        # Should be throttled but still executed (with reduced amount)
        assert result.gated.decision == EnforcementDecision.THROTTLE
        assert result.executed is True
        assert result.params["amount"] == 300


class TestActionMonitor:
    def test_monitor_tracks_cumulative(self):
        registry = ConstraintRegistry()
        registry.register(make_soft_threshold_constraint(300))
        monitor = ActionMonitor(registry)

        # Simulate tracking
        monitor._update_tracking("purchase", {"amount": 100})
        monitor._update_tracking("purchase", {"amount": 200})
        assert monitor.get_cumulative("amount") == 300.0

    def test_monitor_reset(self):
        registry = ConstraintRegistry()
        monitor = ActionMonitor(registry)
        monitor._update_tracking("purchase", {"amount": 100})
        monitor.reset()
        assert monitor.get_cumulative("amount") == 0.0


class TestMixedConstraints:
    def test_hard_takes_precedence_over_soft(self):
        """If both hard and soft are violated, hard wins (block)."""
        registry = ConstraintRegistry()

        # Hard limit at 500
        registry.register(ConstraintSpec(
            name="hard_limit",
            description="Hard limit",
            source=ConstraintSource.REGULATORY,
            constraint_class=ConstraintClass.HARD,
            predicate=lambda ctx: (
                ctx.get("_action_params", {}).get("amount", 0) <= 500,
                {},
            ),
            verification_point=VerificationPoint.PRE_ACTION,
            response_protocol=ResponseProtocol.BLOCK,
        ))

        # Soft limit at 300
        registry.register(make_soft_threshold_constraint(300))

        gate = PreActionGate(registry)
        result = gate.check("purchase", {"amount": 600}, {})
        # Hard violation → block (not throttle)
        assert result.decision == EnforcementDecision.BLOCK

    def test_soft_violation_with_hard_ok(self):
        """Amount exceeds soft but not hard → throttle."""
        registry = ConstraintRegistry()

        registry.register(ConstraintSpec(
            name="hard_limit",
            description="Hard limit",
            source=ConstraintSource.REGULATORY,
            constraint_class=ConstraintClass.HARD,
            predicate=lambda ctx: (
                ctx.get("_action_params", {}).get("amount", 0) <= 500,
                {},
            ),
            verification_point=VerificationPoint.PRE_ACTION,
            response_protocol=ResponseProtocol.BLOCK,
        ))

        registry.register(make_soft_threshold_constraint(300))

        gate = PreActionGate(registry)
        result = gate.check("purchase", {"amount": 400}, {})
        assert result.decision == EnforcementDecision.THROTTLE
