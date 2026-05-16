"""
SARC Attribution-Preserving Trace Trees.

In multi-agent systems, constraints must propagate through delegation chains.
Trace trees preserve the full causal path from root agent to leaf action,
enabling:
  - Constraint propagation (a parent's constraints bind its children)
  - Blame assignment (which agent violated which constraint?)
  - Audit replay (reconstruct the full decision path)

Based on: arXiv:2605.07728
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from .spec import ConstraintResult, ConstraintSpec


# ---------------------------------------------------------------------------
# Trace node — single step in the decision/execution chain
# ---------------------------------------------------------------------------

@dataclass
class TraceNode:
    """A single node in the trace tree representing one decision point."""
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = ""
    action: str = ""
    action_params: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    children: list[TraceNode] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Execution outcome
    executed: bool = False
    blocked: bool = False
    block_reason: str = ""

    def add_child(self, child: TraceNode) -> TraceNode:
        child.parent_id = self.node_id
        self.children.append(child)
        return child

    def lineage(self) -> list[str]:
        """Walk up to root, returning agent IDs (requires tree index)."""
        # This is filled by TraceTree when the node is inserted
        return self.metadata.get("_lineage", [self.agent_id])


# ---------------------------------------------------------------------------
# Trace tree — full execution trace with constraint attribution
# ---------------------------------------------------------------------------

class TraceTree:
    """Attribution-preserving trace tree for multi-agent constraint propagation.

    The tree captures the full delegation chain and all constraint evaluations
    at each step. This enables:
      1. Propagating parent constraints to child agents
      2. Blame assignment when violations occur
      3. Full audit replay
    """

    def __init__(self, root_agent_id: str = "root") -> None:
        self.root = TraceNode(agent_id=root_agent_id, parent_id=None)
        self._index: dict[str, TraceNode] = {self.root.node_id: self.root}
        self._agent_constraints: dict[str, list[str]] = {}
        self._violations: list[dict[str, Any]] = []

    def add_node(
        self,
        agent_id: str,
        action: str,
        action_params: dict[str, Any] | None = None,
        parent_id: str | None = None,
        constraint_results: list[ConstraintResult] | None = None,
    ) -> TraceNode:
        """Add a trace node under the specified parent (or root)."""
        parent = self._index.get(parent_id, self.root) if parent_id else self.root
        node = TraceNode(
            agent_id=agent_id,
            action=action,
            action_params=action_params or {},
            constraint_results=constraint_results or [],
        )
        parent.add_child(node)
        self._index[node.node_id] = node

        # Build lineage
        parent_lineage = parent.metadata.get("_lineage", [parent.agent_id])
        node.metadata["_lineage"] = parent_lineage + [agent_id]

        # Record violations
        for cr in (constraint_results or []):
            if not cr.satisfied:
                self._violations.append({
                    "node_id": node.node_id,
                    "agent_id": agent_id,
                    "constraint": cr.constraint_name,
                    "constraint_class": cr.constraint_class.value,
                    "details": cr.details,
                    "lineage": node.lineage(),
                    "timestamp": cr.timestamp,
                })

        return node

    def mark_executed(self, node_id: str, executed: bool = True) -> None:
        node = self._index.get(node_id)
        if node:
            node.executed = executed

    def mark_blocked(self, node_id: str, reason: str = "") -> None:
        node = self._index.get(node_id)
        if node:
            node.blocked = True
            node.executed = False
            node.block_reason = reason

    def propagate_constraints(
        self,
        parent_agent: str,
        child_agent: str,
        constraints: Sequence[ConstraintSpec],
    ) -> None:
        """Propagate constraints from parent to child agent.

        In SARC, when a parent delegates to a child, the parent's constraints
        bind the child. This method records that binding for audit.
        """
        constraint_names = [c.name for c in constraints]
        existing = self._agent_constraints.get(child_agent, [])
        self._agent_constraints[child_agent] = list(
            set(existing + constraint_names)
        )

    def get_agent_constraints(self, agent_id: str) -> list[str]:
        """Return constraint names bound to an agent (own + inherited)."""
        return self._agent_constraints.get(agent_id, [])

    def get_violations(self) -> list[dict[str, Any]]:
        """Return all recorded violations with full attribution."""
        return list(self._violations)

    def blame(self, constraint_name: str) -> list[dict[str, Any]]:
        """Find all agents responsible for violations of a specific constraint."""
        return [
            v for v in self._violations
            if v["constraint"] == constraint_name
        ]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the trace tree for audit storage."""
        def _node_to_dict(node: TraceNode) -> dict[str, Any]:
            return {
                "node_id": node.node_id,
                "agent_id": node.agent_id,
                "action": node.action,
                "action_params": node.action_params,
                "parent_id": node.parent_id,
                "constraint_results": [
                    {
                        "constraint": cr.constraint_name,
                        "class": cr.constraint_class.value,
                        "satisfied": cr.satisfied,
                        "details": cr.details,
                    }
                    for cr in node.constraint_results
                ],
                "executed": node.executed,
                "blocked": node.blocked,
                "block_reason": node.block_reason,
                "children": [_node_to_dict(c) for c in node.children],
                "timestamp": node.timestamp,
            }

        return {
            "root": _node_to_dict(self.root),
            "agent_constraints": self._agent_constraints,
            "violations": self._violations,
        }

    def node_count(self) -> int:
        return len(self._index)

    def violation_count(self) -> int:
        return len(self._violations)
