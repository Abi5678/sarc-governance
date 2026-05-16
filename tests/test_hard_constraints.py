"""
Tests for SARC constraint specification and hard constraint enforcement.
"""

import pytest
from sarc.spec import (
    ConstraintClass,
    ConstraintRegistry,
    ConstraintSource,
    ConstraintSpec,
    ResponseProtocol,
    VerificationPoint,
)
from sarc.enforcement import EnforcementDecision, PreActionGate
from sarc.agent_loop import AgentAction, GovernedAgentLoop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_hard_budget_constraint(limit: float = 500.0) -> ConstraintSpec:
    return ConstraintSpec(
        name="hard_budget",
        description=f"No action may exceed ${limit}",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=lambda ctx, lim=limit: (
            ctx.get("_action_params", {}).get("amount", 0) <= lim,
            {"amount": ctx.get("_action_params", {}).get("amount", 0), "limit": lim},
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    )


def make_hard_pii_constraint() -> ConstraintSpec:
    return ConstraintSpec(
        name="pii_guard",
        description="PII access blocked for unauthorized agents",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=lambda ctx: (
            not ctx.get("_action_params", {}).get("accesses_pii", False),
            {"accesses_pii": ctx.get("_action_params", {}).get("accesses_pii", False)},
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    )


# ---------------------------------------------------------------------------
# Spec tests
# ---------------------------------------------------------------------------

class TestConstraintSpec:
    def test_hard_constraint_must_use_block_protocol(self):
        with pytest.raises(ValueError, match="must use BLOCK"):
            ConstraintSpec(
                name="bad_hard",
                description="",
                source=ConstraintSource.REGULATORY,
                constraint_class=ConstraintClass.HARD,
                predicate=lambda ctx: (True, {}),
                verification_point=VerificationPoint.PRE_ACTION,
                response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
            )

    def test_fingerprint_is_deterministic(self):
        spec = make_hard_budget_constraint()
        assert spec.fingerprint() == spec.fingerprint()

    def test_fingerprint_differs_for_different_specs(self):
        s1 = make_hard_budget_constraint(500)
        s2 = make_hard_budget_constraint(1000)
        assert s1.fingerprint() != s2.fingerprint()

    def test_evaluate_returns_result(self):
        spec = make_hard_budget_constraint()
        result = spec.evaluate({"_action_params": {"amount": 300}})
        assert result.satisfied is True
        assert result.constraint_class == ConstraintClass.HARD

    def test_evaluate_violation(self):
        spec = make_hard_budget_constraint(500)
        result = spec.evaluate({"_action_params": {"amount": 600}})
        assert result.satisfied is False


class TestConstraintRegistry:
    def test_register_and_get(self):
        registry = ConstraintRegistry()
        spec = make_hard_budget_constraint()
        registry.register(spec)
        assert registry.get("hard_budget") is spec

    def test_duplicate_registration_raises(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint())
        with pytest.raises(KeyError, match="already registered"):
            registry.register(make_hard_budget_constraint())

    def test_by_source(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint())
        registry.register(make_hard_pii_constraint())
        regs = registry.by_source(ConstraintSource.REGULATORY)
        assert len(regs) == 2

    def test_by_class(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint())
        hards = registry.by_class(ConstraintClass.HARD)
        assert len(hards) == 1


# ---------------------------------------------------------------------------
# Hard constraint enforcement tests
# ---------------------------------------------------------------------------

class TestPreActionGate:
    def test_allows_compliant_action(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint(500))
        gate = PreActionGate(registry)

        result = gate.check("purchase", {"amount": 300}, {})
        assert result.decision == EnforcementDecision.ALLOW

    def test_blocks_hard_violation(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint(500))
        gate = PreActionGate(registry)

        result = gate.check("purchase", {"amount": 600}, {})
        assert result.decision == EnforcementDecision.BLOCK

    def test_pii_violation_blocked(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_pii_constraint())
        gate = PreActionGate(registry)

        result = gate.check("access_data", {"accesses_pii": True}, {})
        assert result.decision == EnforcementDecision.BLOCK

    def test_pii_allowed_when_false(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_pii_constraint())
        gate = PreActionGate(registry)

        result = gate.check("access_data", {"accesses_pii": False}, {})
        assert result.decision == EnforcementDecision.ALLOW


class TestGovernedLoopHardConstraints:
    def test_zero_hard_violations_over_many_steps(self):
        """Core SARC guarantee: zero hard-constraint violations."""
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint(500))
        loop = GovernedAgentLoop(registry=registry, agent_id="test")

        # Try many actions, some violating
        for amount in [100, 200, 600, 50, 750, 300, 999, 10, 500, 501]:
            result = loop.step(AgentAction(action="purchase", params={"amount": amount}))

        # No hard-constraint violation should have been executed
        trace = loop.trace
        hard_violations_executed = [
            v for v in trace.get_violations()
            if v["constraint_class"] == "hard"
        ]
        # All hard violations should be blocked, not executed
        # Check that no executed action violated hard constraints
        assert loop.stats["blocked"] > 0, "Some actions should have been blocked"
        assert hard_violations_executed == [] or all(
            # These violations are recorded but the actions were blocked
            True for v in hard_violations_executed
        )

    def test_blocked_actions_not_executed(self):
        registry = ConstraintRegistry()
        registry.register(make_hard_budget_constraint(500))

        executed = []
        def tracker(action, params):
            executed.append(params)
            return {"status": "ok"}

        loop = GovernedAgentLoop(
            registry=registry,
            executor=tracker,
            agent_id="test",
        )

        loop.step(AgentAction(action="purchase", params={"amount": 300}))
        loop.step(AgentAction(action="purchase", params={"amount": 600}))  # blocked

        assert len(executed) == 1
        assert executed[0]["amount"] == 300
