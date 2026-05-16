"""
SARC Agent Loop — Governance-Integrated Agent Execution.

This module wraps the four enforcement sites into a single coherent agent
loop. Every action the agent proposes passes through:

  1. Pre-action gate → allow / block / throttle
  2. Action execution (if allowed)
  3. Action-time monitoring (during execution)
  4. Post-action audit (after execution)
  5. Escalation routing (if any site triggers it)

The key architectural commitment: constraints are evaluated at structurally
guaranteed points, not at the whim of the agent or its framework.

Based on: arXiv:2605.07728
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .enforcement import (
    ActionMonitor,
    EnforcementDecision,
    EnforcementResult,
    EscalationRouter,
    PostActionAuditor,
    PreActionGate,
)
from .spec import ConstraintRegistry, ConstraintSpec, VerificationPoint
from .trace import TraceNode, TraceTree


# ---------------------------------------------------------------------------
# Action executor protocol
# ---------------------------------------------------------------------------

class ActionExecutor(Protocol):
    """Protocol for executing agent actions.

    Implement this to plug in any agent framework (CrewAI, LangGraph,
    AutoGen, etc.). The executor receives the (possibly throttled) action
    parameters and returns the outcome.
    """
    def __call__(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...


# Type alias
ActionExecutorFn = Callable[[str, dict[str, Any]], dict[str, Any]]


# ---------------------------------------------------------------------------
# Agent action — what the agent proposes to do
# ---------------------------------------------------------------------------

@dataclass
class AgentAction:
    """A proposed action from the agent."""
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    agent_id: str = "default"


# ---------------------------------------------------------------------------
# Step result — outcome of one step through the governed loop
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Result of processing one action through the governance loop."""
    action: str
    params: dict[str, Any]
    gated: EnforcementResult          # Pre-action gate result
    monitored: EnforcementResult | None = None  # Action-time monitor result
    audited: EnforcementResult | None = None    # Post-action audit result
    executed: bool = False
    outcome: dict[str, Any] | None = None
    trace_node_id: str | None = None


# ---------------------------------------------------------------------------
# Governed agent loop
# ---------------------------------------------------------------------------

class GovernedAgentLoop:
    """The core SARC agent loop with integrated governance.

    Usage:
        registry = ConstraintRegistry()
        registry.register(budget_constraint)
        registry.register(data_access_constraint)

        loop = GovernedAgentLoop(registry)
        result = loop.step(AgentAction(action="purchase", params={"amount": 500}))

    The loop guarantees that every action passes through all applicable
    enforcement sites before, during, and after execution.
    """

    def __init__(
        self,
        registry: ConstraintRegistry,
        executor: ActionExecutorFn | None = None,
        escalation_router: EscalationRouter | None = None,
        trace: TraceTree | None = None,
        agent_id: str = "default",
    ) -> None:
        self.registry = registry
        self.pre_gate = PreActionGate(registry)
        self.monitor = ActionMonitor(registry)
        self.auditor = PostActionAuditor(registry)
        self.escalation_router = escalation_router or EscalationRouter()
        self.trace = trace or TraceTree(root_agent_id=agent_id)
        self.agent_id = agent_id

        # Default executor: no-op
        self._executor = executor or self._noop_executor

        # Stats
        self._steps: int = 0
        self._blocked: int = 0
        self._throttled: int = 0
        self._escalated: int = 0

    def step(self, action: AgentAction) -> StepResult:
        """Process one action through the full governance loop."""
        self._steps += 1
        result = StepResult(
            action=action.action,
            params=action.params,
            gated=EnforcementResult(decision=EnforcementDecision.ALLOW),
        )

        # Build evaluation context
        context = self._build_context(action)

        # --- PHASE 1: Pre-action gate ---
        result.gated = self.pre_gate.check(
            action=action.action,
            action_params=action.params,
            context=context,
        )

        if result.gated.decision == EnforcementDecision.BLOCK:
            self._blocked += 1
            # Record in trace
            node = self.trace.add_node(
                agent_id=action.agent_id,
                action=action.action,
                action_params=action.params,
                constraint_results=result.gated.constraint_results,
            )
            self.trace.mark_blocked(node.node_id, reason=result.gated.reason)
            result.trace_node_id = node.node_id
            return result

        if result.gated.decision == EnforcementDecision.ESCALATE:
            self._escalated += 1
            # Route to escalation and use that decision
            for cr in result.gated.constraint_results:
                if not cr.satisfied:
                    esc_result = self.escalation_router.route(cr, context, self.trace)
                    if esc_result.decision == EnforcementDecision.BLOCK:
                        self._blocked += 1
                        node = self.trace.add_node(
                            agent_id=action.agent_id,
                            action=action.action,
                            action_params=action.params,
                            constraint_results=result.gated.constraint_results,
                        )
                        self.trace.mark_blocked(node.node_id, reason=esc_result.reason)
                        result.trace_node_id = node.node_id
                        result.gated = esc_result
                        return result

        # --- PHASE 2: Prepare execution params ---
        exec_params = action.params
        if result.gated.decision == EnforcementDecision.THROTTLE:
            self._throttled += 1
            if result.gated.throttled_params is not None:
                exec_params = result.gated.throttled_params
                result.params = exec_params

        # --- PHASE 3: Execute ---
        outcome = self._executor(action.action, exec_params)
        result.executed = True
        result.outcome = outcome

        # --- PHASE 4: Action-time monitoring ---
        result.monitored = self.monitor.check(
            action=action.action,
            action_params=exec_params,
            context={**context, "_outcome": outcome},
        )

        if result.monitored.decision == EnforcementDecision.ESCALATE:
            self._escalated += 1
            for cr in result.monitored.constraint_results:
                if not cr.satisfied:
                    self.escalation_router.route(cr, context, self.trace)

        # --- PHASE 5: Post-action audit ---
        result.audited = self.auditor.audit(
            action=action.action,
            action_params=exec_params,
            outcome=outcome,
            context=context,
        )

        if result.audited.decision in (
            EnforcementDecision.ROLLBACK,
            EnforcementDecision.ESCALATE,
        ):
            for cr in result.audited.constraint_results:
                if not cr.satisfied:
                    self.escalation_router.route(cr, context, self.trace)

        # --- PHASE 6: Record in trace ---
        all_results = list(result.gated.constraint_results)
        if result.monitored:
            all_results.extend(result.monitored.constraint_results)
        if result.audited:
            all_results.extend(result.audited.constraint_results)

        node = self.trace.add_node(
            agent_id=action.agent_id,
            action=action.action,
            action_params=exec_params,
            constraint_results=all_results,
        )
        self.trace.mark_executed(node.node_id)
        result.trace_node_id = node.node_id

        return result

    def run(
        self,
        actions: Sequence[AgentAction],
    ) -> list[StepResult]:
        """Run a sequence of actions through the governed loop."""
        return [self.step(action) for action in actions]

    def _build_context(self, action: AgentAction) -> dict[str, Any]:
        """Build the evaluation context for constraint predicates."""
        return {
            "_agent_id": action.agent_id,
            "_step": self._steps,
            "_cumulative": dict(self.monitor._cumulative),
        }

    @staticmethod
    def _noop_executor(action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Default no-op executor that returns empty outcome."""
        return {"status": "noop", "action": action}

    # --- Stats ---

    @property
    def stats(self) -> dict[str, int]:
        return {
            "steps": self._steps,
            "blocked": self._blocked,
            "throttled": self._throttled,
            "escalated": self._escalated,
        }
