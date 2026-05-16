"""
Example: Single-Agent Procurement Task with Budget Constraints.

This example demonstrates SARC's core value proposition: a procurement
agent that must stay within budget constraints. Hard constraints (regulatory
spending limits) are enforced at the pre-action gate, while soft constraints
(preferred spending thresholds) are monitored and throttled.

Run: python -m examples.single_agent_procurement
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure package is importable when running from examples/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sarc import (
    AgentAction,
    ConstraintClass,
    ConstraintRegistry,
    ConstraintSource,
    ConstraintSpec,
    GovernedAgentLoop,
    OperatingPoint,
    ResponseProtocol,
    VerificationPoint,
)


def create_procurement_constraints(
    per_action_limit: float = 500.0,
    daily_budget: float = 5000.0,
    preferred_threshold: float = 300.0,
) -> ConstraintRegistry:
    """Create the constraint registry for a procurement agent."""
    registry = ConstraintRegistry()

    # HARD: No single purchase may exceed per-action limit
    registry.register(ConstraintSpec(
        name="per_purchase_limit",
        description=f"No single purchase may exceed ${per_action_limit:,.0f}",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=lambda ctx: (
            ctx.get("_action_params", {}).get("amount", 0) <= per_action_limit,
            {
                "amount": ctx.get("_action_params", {}).get("amount", 0),
                "limit": per_action_limit,
            },
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    ))

    # HARD: Total spending may not exceed daily budget
    registry.register(ConstraintSpec(
        name="daily_budget_ceiling",
        description=f"Total daily spending may not exceed ${daily_budget:,.0f}",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=lambda ctx: (
            ctx.get("_cumulative", {}).get("amount", 0)
            + ctx.get("_action_params", {}).get("amount", 0)
            <= daily_budget,
            {
                "cumulative": ctx.get("_cumulative", {}).get("amount", 0),
                "proposed": ctx.get("_action_params", {}).get("amount", 0),
                "ceiling": daily_budget,
            },
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    ))

    # SOFT: Preferred spending threshold per action
    registry.register(ConstraintSpec(
        name="preferred_spend_threshold",
        description=f"Preferred per-action spending ≤ ${preferred_threshold:,.0f}",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx: (
            ctx.get("_action_params", {}).get("amount", 0) <= preferred_threshold,
            {
                "amount": ctx.get("_action_params", {}).get("amount", 0),
                "threshold": preferred_threshold,
            },
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.THROTTLE,
        operating_point=OperatingPoint(threshold=preferred_threshold),
    ))

    # SOFT: Post-action vendor diversity check
    registry.register(ConstraintSpec(
        name="vendor_diversity",
        description="Avoid spending >60% of budget with a single vendor",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx: (
            ctx.get("_outcome", {}).get("vendor_concentration", 0) <= 0.6,
            {"concentration": ctx.get("_outcome", {}).get("vendor_concentration", 0)},
        ),
        verification_point=VerificationPoint.POST_ACTION,
        response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
    ))

    return registry


# Simple executor simulating purchase outcomes
def purchase_executor(action: str, params: dict) -> dict:
    """Simulate a purchase action."""
    if action == "purchase":
        return {
            "status": "completed",
            "vendor": params.get("vendor", "unknown"),
            "amount_charged": params.get("amount", 0),
            "vendor_concentration": params.get("vendor_concentration", 0.3),
        }
    return {"status": "unknown_action"}


def main() -> None:
    print("=" * 60)
    print("SARC — Single-Agent Procurement Example")
    print("=" * 60)

    registry = create_procurement_constraints()
    loop = GovernedAgentLoop(
        registry=registry,
        executor=purchase_executor,
        agent_id="procurement_agent",
    )

    # Simulate a day of procurement actions
    actions = [
        AgentAction(action="purchase", params={"amount": 250, "vendor": "Acme Corp", "vendor_concentration": 0.2}),
        AgentAction(action="purchase", params={"amount": 450, "vendor": "Acme Corp", "vendor_concentration": 0.4}),  # Exceeds soft threshold
        AgentAction(action="purchase", params={"amount": 600, "vendor": "Beta Inc", "vendor_concentration": 0.3}),  # Exceeds hard limit!
        AgentAction(action="purchase", params={"amount": 150, "vendor": "Gamma LLC", "vendor_concentration": 0.2}),
        AgentAction(action="purchase", params={"amount": 350, "vendor": "Acme Corp", "vendor_concentration": 0.7}),  # Exceeds soft, high concentration
        AgentAction(action="purchase", params={"amount": 200, "vendor": "Delta Co", "vendor_concentration": 0.3}),
        AgentAction(action="purchase", params={"amount": 4800, "vendor": "Omega Ltd", "vendor_concentration": 0.1}),  # Would exceed daily budget!
    ]

    print(f"\nRunning {len(actions)} procurement actions...")
    print("-" * 60)

    results = loop.run(actions)

    for i, (action, result) in enumerate(zip(actions, results)):
        status = "✅ ALLOWED" if result.executed else "🚫 BLOCKED"
        if result.gated.decision.value == "throttle":
            status = "⚡ THROTTLED"

        print(f"\n[{i+1}] ${action.params['amount']:,.0f} → {action.params['vendor']}")
        print(f"    {status}")
        if result.gated.reason:
            print(f"    Reason: {result.gated.reason}")
        if result.gated.throttled_params and result.gated.throttled_params.get("amount") != action.params.get("amount"):
            original = action.params.get("amount", 0)
            throttled = result.gated.throttled_params.get("amount", 0)
            print(f"    Throttled: ${original:,.0f} → ${throttled:,.0f}")

    # Summary
    print("\n" + "=" * 60)
    print("Session Summary")
    print("-" * 60)
    stats = loop.stats
    print(f"  Total steps:   {stats['steps']}")
    print(f"  Allowed:       {stats['allowed']}")
    print(f"  Blocked:       {stats['blocked']}")
    print(f"  Throttled:     {stats['throttled']}")
    print(f"  Escalated:     {stats['escalated']}")

    # Trace tree
    print(f"\n  Trace nodes:   {loop.trace.node_count()}")
    print(f"  Violations:    {loop.trace.violation_count()}")

    # Verify zero hard-constraint violations
    hard_violations = [
        v for v in loop.trace.get_violations()
        if v["constraint_class"] == "hard"
    ]
    print(f"  Hard violations executed: {len(hard_violations)}")
    assert len(hard_violations) == 0, "Hard constraint violations should be zero!"
    print("\n  ✅ Zero hard-constraint violations confirmed!")

    # Audit
    from sarc import AuditChecker
    checker = AuditChecker()
    checker.record_all(results)
    audit_stats = checker.compute_stats()
    print(f"\n  Audit: {audit_stats.hard_violations} hard, {audit_stats.soft_violations} soft violations")
    print(f"  Zero hard violations: {audit_stats.zero_hard_violations}")


if __name__ == "__main__":
    main()
