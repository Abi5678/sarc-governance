"""
Tests for SARC escalation routing and trace attribution.
"""

import pytest
from sarc.spec import (
    ConstraintClass,
    ConstraintRegistry,
    ConstraintSource,
    ConstraintSpec,
    ConstraintResult,
    ResponseProtocol,
    VerificationPoint,
)
from sarc.enforcement import (
    EnforcementDecision,
    EscalationRouter,
    PostActionAuditor,
)
from sarc.trace import TraceNode, TraceTree


# ---------------------------------------------------------------------------
# Escalation tests
# ---------------------------------------------------------------------------

class TestEscalationRouter:
    def test_default_handler_blocks_hard(self):
        router = EscalationRouter()
        result = ConstraintResult(
            constraint_name="test",
            constraint_class=ConstraintClass.HARD,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.REGULATORY,
            response_protocol=ResponseProtocol.BLOCK,
        )
        decision = router._default_handler(result, {}, None)
        assert decision == EnforcementDecision.BLOCK

    def test_default_handler_allows_soft(self):
        router = EscalationRouter()
        result = ConstraintResult(
            constraint_name="test",
            constraint_class=ConstraintClass.SOFT,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.ORGANIZATIONAL,
            response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
        )
        decision = router._default_handler(result, {}, None)
        assert decision == EnforcementDecision.ALLOW

    def test_auto_response(self):
        router = EscalationRouter()
        router.set_auto_response("test_constraint", EnforcementDecision.BLOCK)

        result = ConstraintResult(
            constraint_name="test_constraint",
            constraint_class=ConstraintClass.SOFT,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.ORGANIZATIONAL,
            response_protocol=ResponseProtocol.ESCALATE,
        )
        esc_result = router.route(result, {"_agent_id": "test_agent"})
        assert esc_result.decision == EnforcementDecision.BLOCK

    def test_custom_handler(self):
        decisions = []

        def custom_handler(result, context, trace):
            decisions.append(result.constraint_name)
            return EnforcementDecision.THROTTLE

        router = EscalationRouter(handler=custom_handler)
        result = ConstraintResult(
            constraint_name="custom_test",
            constraint_class=ConstraintClass.SOFT,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.ORGANIZATIONAL,
            response_protocol=ResponseProtocol.ESCALATE,
        )
        esc_result = router.route(result, {})
        assert esc_result.decision == EnforcementDecision.THROTTLE
        assert "custom_test" in decisions

    def test_resolve_escalation(self):
        router = EscalationRouter()
        result = ConstraintResult(
            constraint_name="test",
            constraint_class=ConstraintClass.HARD,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.REGULATORY,
            response_protocol=ResponseProtocol.ESCALATE,
        )
        esc_result = router.route(result, {})
        esc_id = esc_result.escalation_id
        assert esc_id is not None

        # Manually resolve
        router.resolve(esc_id, EnforcementDecision.BLOCK, "human rejected")
        pending = router.get_pending_escalations()
        assert len(pending) == 0


class TestPostActionAuditor:
    def test_audit_allows_compliant(self):
        registry = ConstraintRegistry()
        registry.register(ConstraintSpec(
            name="post_check",
            description="Post-action check",
            source=ConstraintSource.ORGANIZATIONAL,
            constraint_class=ConstraintClass.SOFT,
            predicate=lambda ctx: (
                ctx.get("_outcome", {}).get("status") == "ok",
                {},
            ),
            verification_point=VerificationPoint.POST_ACTION,
            response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
        ))
        auditor = PostActionAuditor(registry)
        result = auditor.audit("action", {}, {"status": "ok"}, {})
        assert result.decision == EnforcementDecision.ALLOW

    def test_audit_catches_violation(self):
        registry = ConstraintRegistry()
        registry.register(ConstraintSpec(
            name="post_check",
            description="Post-action check",
            source=ConstraintSource.ORGANIZATIONAL,
            constraint_class=ConstraintClass.SOFT,
            predicate=lambda ctx: (
                ctx.get("_outcome", {}).get("status") == "ok",
                {},
            ),
            verification_point=VerificationPoint.POST_ACTION,
            response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
        ))
        auditor = PostActionAuditor(registry)
        result = auditor.audit("action", {}, {"status": "error"}, {})
        # Soft violation → logged but allowed (already executed)
        assert result.decision == EnforcementDecision.ALLOW

    def test_audit_log_recorded(self):
        registry = ConstraintRegistry()
        registry.register(ConstraintSpec(
            name="post_check",
            description="Post-action check",
            source=ConstraintSource.ORGANIZATIONAL,
            constraint_class=ConstraintClass.SOFT,
            predicate=lambda ctx: (True, {}),
            verification_point=VerificationPoint.POST_ACTION,
            response_protocol=ResponseProtocol.LOG_AND_CONTINUE,
        ))
        auditor = PostActionAuditor(registry)
        auditor.audit("action1", {}, {}, {})
        auditor.audit("action2", {}, {}, {})
        assert len(auditor.get_audit_log()) == 2


# ---------------------------------------------------------------------------
# Trace tree tests
# ---------------------------------------------------------------------------

class TestTraceTree:
    def test_add_node(self):
        tree = TraceTree(root_agent_id="root")
        node = tree.add_node(agent_id="worker", action="process")
        assert node.agent_id == "worker"
        assert node.parent_id == tree.root.node_id

    def test_lineage(self):
        tree = TraceTree(root_agent_id="manager")
        child = tree.add_node(agent_id="worker", action="task1")
        grandchild = tree.add_node(
            agent_id="subworker", action="subtask", parent_id=child.node_id
        )
        assert child.lineage() == ["manager", "worker"]
        assert grandchild.lineage() == ["manager", "worker", "subworker"]

    def test_violation_tracking(self):
        tree = TraceTree(root_agent_id="root")
        result = ConstraintResult(
            constraint_name="test",
            constraint_class=ConstraintClass.HARD,
            satisfied=False,
            details={"reason": "exceeded"},
            timestamp=0,
            source=ConstraintSource.REGULATORY,
            response_protocol=ResponseProtocol.BLOCK,
        )
        tree.add_node(
            agent_id="agent1",
            action="purchase",
            constraint_results=[result],
        )
        violations = tree.get_violations()
        assert len(violations) == 1
        assert violations[0]["constraint"] == "test"
        assert violations[0]["agent_id"] == "agent1"

    def test_blame_assignment(self):
        tree = TraceTree(root_agent_id="root")
        r1 = ConstraintResult(
            constraint_name="budget",
            constraint_class=ConstraintClass.HARD,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.REGULATORY,
            response_protocol=ResponseProtocol.BLOCK,
        )
        r2 = ConstraintResult(
            constraint_name="budget",
            constraint_class=ConstraintClass.HARD,
            satisfied=False,
            details={},
            timestamp=0,
            source=ConstraintSource.REGULATORY,
            response_protocol=ResponseProtocol.BLOCK,
        )
        tree.add_node(agent_id="agent_a", action="buy", constraint_results=[r1])
        tree.add_node(agent_id="agent_b", action="buy", constraint_results=[r2])

        blame = tree.blame("budget")
        assert len(blame) == 2
        agents = {b["agent_id"] for b in blame}
        assert agents == {"agent_a", "agent_b"}

    def test_propagate_constraints(self):
        tree = TraceTree(root_agent_id="manager")
        from sarc.spec import ConstraintSpec, ConstraintSource, ConstraintClass, ResponseProtocol, VerificationPoint
        c1 = ConstraintSpec(
            name="c1",
            description="",
            source=ConstraintSource.REGULATORY,
            constraint_class=ConstraintClass.HARD,
            predicate=lambda ctx: (True, {}),
            verification_point=VerificationPoint.PRE_ACTION,
            response_protocol=ResponseProtocol.BLOCK,
        )
        tree.propagate_constraints("manager", "worker", [c1])
        inherited = tree.get_agent_constraints("worker")
        assert "c1" in inherited

    def test_to_dict_serialization(self):
        tree = TraceTree(root_agent_id="root")
        tree.add_node(agent_id="agent1", action="task1")
        d = tree.to_dict()
        assert "root" in d
        assert "violations" in d
        assert len(d["root"]["children"]) == 1
