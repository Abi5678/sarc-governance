"""
Example: Multi-Agent Workflow with Constraint Propagation.

This example demonstrates SARC in a multi-agent setting where a manager
agent delegates to worker agents. Constraints propagate from parent to
child agents through the trace tree, ensuring that a manager's regulatory
constraints bind all delegated actions.

Run: python -m examples.multi_agent_workflow
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sarc import (
    AgentAction,
    ConstraintClass,
    ConstraintRegistry,
    ConstraintSource,
    ConstraintSpec,
    ConstraintResult,
    GovernedAgentLoop,
    OperatingPoint,
    ResponseProtocol,
    TraceTree,
    VerificationPoint,
)


def create_org_constraints(
    data_access_limit: float = 100.0,
    budget_limit: float = 2000.0,
) -> ConstraintRegistry:
    """Create organizational constraints that propagate to all agents."""
    registry = ConstraintRegistry()

    # HARD: No PII access without authorization
    registry.register(ConstraintSpec(
        name="pii_access_guard",
        description="PII data may only be accessed by authorized agents",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=lambda ctx: (
            not ctx.get("_action_params", {}).get("accesses_pii", False)
            or ctx.get("_agent_id", "").startswith("authorized_"),
            {
                "accesses_pii": ctx.get("_action_params", {}).get("accesses_pii", False),
                "agent": ctx.get("_agent_id", ""),
            },
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    ))

    # HARD: Budget ceiling for the entire workflow
    registry.register(ConstraintSpec(
        name="workflow_budget_ceiling",
        description=f"Total workflow spending ≤ ${budget_limit:,.0f}",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=lambda ctx: (
            ctx.get("_cumulative", {}).get("amount", 0)
            + ctx.get("_action_params", {}).get("amount", 0)
            <= budget_limit,
            {
                "cumulative": ctx.get("_cumulative", {}).get("amount", 0),
                "proposed": ctx.get("_action_params", {}).get("amount", 0),
                "ceiling": budget_limit,
            },
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    ))

    # SOFT: Preferred data access limit per action
    registry.register(ConstraintSpec(
        name="data_access_limit",
        description=f"Preferred data access ≤ {data_access_limit} records per action",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx: (
            ctx.get("_action_params", {}).get("records_accessed", 0) <= data_access_limit,
            {
                "records": ctx.get("_action_params", {}).get("records_accessed", 0),
                "limit": data_access_limit,
            },
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.THROTTLE,
        operating_point=OperatingPoint(threshold=data_access_limit),
    ))

    # SOFT: Post-action data sensitivity check
    registry.register(ConstraintSpec(
        name="data_sensitivity_audit",
        description="Audit data sensitivity levels of accessed records",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx: (
            ctx.get("_outcome", {}).get("max_sensitivity", "low") in ("low", "medium"),
            {"sensitivity": ctx.get("_outcome", {}).get("max_sensitivity", "low")},
        ),
        verification_point=VerificationPoint.POST_ACTION,
        response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
    ))

    return registry


def workflow_executor(action: str, params: dict) -> dict:
    """Simulate workflow actions."""
    if action == "analyze_data":
        return {
            "status": "completed",
            "records_analyzed": params.get("records_accessed", 0),
            "max_sensitivity": params.get("sensitivity", "low"),
            "cost": params.get("amount", 0),
        }
    elif action == "generate_report":
        return {
            "status": "completed",
            "report_id": "rpt-001",
            "cost": params.get("amount", 0),
        }
    return {"status": "unknown_action"}


def main() -> None:
    print("=" * 60)
    print("SARC — Multi-Agent Workflow Example")
    print("=" * 60)

    # Create shared constraint registry and trace tree
    registry = create_org_constraints()
    trace = TraceTree(root_agent_id="manager")

    # Propagate manager's constraints to worker agents
    all_constraints = registry.all_constraints()
    trace.propagate_constraints("manager", "authorized_analyst", all_constraints)
    trace.propagate_constraints("manager", "junior_analyst", all_constraints)
    trace.propagate_constraints("manager", "external_contractor", all_constraints)

    # Create governed loop
    loop = GovernedAgentLoop(
        registry=registry,
        executor=workflow_executor,
        trace=trace,
        agent_id="manager",
    )

    print("\nManager agent delegates tasks to analysts...")
    print("-" * 60)

    # Simulate multi-agent workflow
    actions = [
        # Authorized analyst — can access PII
        AgentAction(
            action="analyze_data",
            params={
                "amount": 300,
                "records_accessed": 80,
                "accesses_pii": True,
                "sensitivity": "medium",
                "vendor_concentration": 0.3,
            },
            agent_id="authorized_analyst",
        ),
        # Junior analyst — tries to access PII (should be BLOCKED)
        AgentAction(
            action="analyze_data",
            params={
                "amount": 200,
                "records_accessed": 50,
                "accesses_pii": True,
                "sensitivity": "high",
                "vendor_concentration": 0.2,
            },
            agent_id="junior_analyst",
        ),
        # Junior analyst — no PII, but too many records (soft throttle)
        AgentAction(
            action="analyze_data",
            params={
                "amount": 150,
                "records_accessed": 150,
                "accesses_pii": False,
                "sensitivity": "low",
                "vendor_concentration": 0.1,
            },
            agent_id="junior_analyst",
        ),
        # External contractor — no PII, within limits
        AgentAction(
            action="generate_report",
            params={
                "amount": 500,
                "records_accessed": 30,
                "accesses_pii": False,
                "sensitivity": "low",
                "vendor_concentration": 0.1,
            },
            agent_id="external_contractor",
        ),
        # External contractor — tries PII (BLOCKED)
        AgentAction(
            action="analyze_data",
            params={
                "amount": 400,
                "records_accessed": 60,
                "accesses_pii": True,
                "sensitivity": "high",
                "vendor_concentration": 0.4,
            },
            agent_id="external_contractor",
        ),
        # Authorized analyst — within limits
        AgentAction(
            action="generate_report",
            params={
                "amount": 350,
                "records_accessed": 40,
                "accesses_pii": False,
                "sensitivity": "medium",
                "vendor_concentration": 0.2,
            },
            agent_id="authorized_analyst",
        ),
    ]

    results = loop.run(actions)

    for i, (action, result) in enumerate(zip(actions, results)):
        status = "✅ ALLOWED" if result.executed else "🚫 BLOCKED"
        if result.gated.decision.value == "throttle":
            status = "⚡ THROTTLED"

        agent_label = action.agent_id.replace("_", " ").title()
        print(f"\n[{i+1}] {agent_label}: {action.action}")
        print(f"    Amount: ${action.params.get('amount', 0):,.0f} | "
              f"Records: {action.params.get('records_accessed', 0)} | "
              f"PII: {action.params.get('accesses_pii', False)}")
        print(f"    {status}")
        if result.gated.reason:
            print(f"    Reason: {result.gated.reason}")

    # Constraint propagation summary
    print("\n" + "=" * 60)
    print("Constraint Propagation")
    print("-" * 60)
    for agent in ("authorized_analyst", "junior_analyst", "external_contractor"):
        constraints = trace.get_agent_constraints(agent)
        print(f"  {agent}: {len(constraints)} inherited constraints")
        for c in constraints:
            print(f"    - {c}")

    # Blame assignment
    print("\n" + "=" * 60)
    print("Violation Attribution")
    print("-" * 60)
    violations = trace.get_violations()
    for v in violations:
        print(f"  Agent: {v['agent_id']}")
        print(f"  Constraint: {v['constraint']} ({v['constraint_class']})")
        print(f"  Lineage: {' → '.join(v['lineage'])}")
        print()

    # Verify zero hard-constraint violations in executed actions
    hard_executed = [v for v in violations if v["constraint_class"] == "hard"]
    print(f"Hard constraint violations in executed actions: {len(hard_executed)}")
    assert len(hard_executed) == 0, "Hard constraint violations should be zero!"
    print("\n✅ Zero hard-constraint violations confirmed!")

    # Stats
    stats = loop.stats
    print(f"\nSession: {stats['steps']} steps, {stats['blocked']} blocked, "
          f"{stats['throttled']} throttled")


if __name__ == "__main__":
    main()
