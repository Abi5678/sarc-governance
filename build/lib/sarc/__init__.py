"""
SARC — Governance-by-Architecture for Agentic AI

Runtime constraint enforcement layer that compiles governance constraints
into four enforcement sites in the agent loop:
  1. Pre-Action Gate
  2. Action-Time Monitor
  3. Post-Action Auditor
  4. Escalation Router

Based on: arXiv:2605.07728
"""

from .spec import (
    ConstraintClass,
    ConstraintRegistry,
    ConstraintResult,
    ConstraintSource,
    ConstraintSpec,
    OperatingPoint,
    ResponseProtocol,
    VerificationPoint,
)
from .enforcement import (
    ActionMonitor,
    EnforcementDecision,
    EnforcementResult,
    EscalationRouter,
    PostActionAuditor,
    PreActionGate,
)
from .agent_loop import AgentAction, GovernedAgentLoop, StepResult
from .trace import TraceNode, TraceTree
from .audit import AuditChecker, AuditStats, BenchmarkConfig, run_synthetic_benchmark

__version__ = "0.1.0"

__all__ = [
    # Spec
    "ConstraintClass",
    "ConstraintRegistry",
    "ConstraintResult",
    "ConstraintSource",
    "ConstraintSpec",
    "OperatingPoint",
    "ResponseProtocol",
    "VerificationPoint",
    # Enforcement
    "ActionMonitor",
    "EnforcementDecision",
    "EnforcementResult",
    "EscalationRouter",
    "PostActionAuditor",
    "PreActionGate",
    # Agent loop
    "AgentAction",
    "GovernedAgentLoop",
    "StepResult",
    # Trace
    "TraceNode",
    "TraceTree",
    # Audit
    "AuditChecker",
    "AuditStats",
    "BenchmarkConfig",
    "run_synthetic_benchmark",
]
