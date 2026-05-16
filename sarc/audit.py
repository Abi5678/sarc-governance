"""
SARC Audit Log Checker — Reproducible Evaluation.

Provides tools for:
  1. Verifying audit trail integrity
  2. Checking constraint compliance statistics
  3. Reproducing the paper's synthetic evaluation

Based on: arXiv:2605.07728
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .agent_loop import AgentAction, GovernedAgentLoop, StepResult
from .enforcement import EnforcementDecision, PostActionAuditor
from .spec import (
    ConstraintClass,
    ConstraintRegistry,
    ConstraintResult,
    ConstraintSpec,
)
from .trace import TraceTree


# ---------------------------------------------------------------------------
# Audit statistics
# ---------------------------------------------------------------------------

@dataclass
class AuditStats:
    """Summary statistics from an audit pass."""
    total_steps: int = 0
    allowed: int = 0
    blocked: int = 0
    throttled: int = 0
    escalated: int = 0
    rolled_back: int = 0
    hard_violations: int = 0
    soft_violations: int = 0
    zero_hard_violations: bool = True
    soft_window_overage_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_steps": self.total_steps,
            "allowed": self.allowed,
            "blocked": self.blocked,
            "throttled": self.throttled,
            "escalated": self.escalated,
            "rolled_back": self.rolled_back,
            "hard_violations": self.hard_violations,
            "soft_violations": self.soft_violations,
            "zero_hard_violations": self.zero_hard_violations,
            "soft_window_overage_pct": round(self.soft_window_overage_pct, 2),
        }


# ---------------------------------------------------------------------------
# Audit checker
# ---------------------------------------------------------------------------

class AuditChecker:
    """Check and summarize audit trails from governed agent runs."""

    def __init__(self) -> None:
        self._step_results: list[StepResult] = []

    def record(self, result: StepResult) -> None:
        self._step_results.append(result)

    def record_all(self, results: Sequence[StepResult]) -> None:
        self._step_results.extend(results)

    def compute_stats(self) -> AuditStats:
        """Compute summary statistics from recorded step results."""
        stats = AuditStats(total_steps=len(self._step_results))

        hard_violations = 0
        soft_violations = 0
        soft_overages = 0
        soft_total = 0

        for sr in self._step_results:
            if sr.gated.decision == EnforcementDecision.BLOCK:
                stats.blocked += 1
            elif sr.gated.decision == EnforcementDecision.THROTTLE:
                stats.throttled += 1
                stats.allowed += 1
            elif sr.gated.decision == EnforcementDecision.ALLOW:
                stats.allowed += 1
            elif sr.gated.decision == EnforcementDecision.ESCALATE:
                stats.escalated += 1

            if sr.audited:
                if sr.audited.decision == EnforcementDecision.ROLLBACK:
                    stats.rolled_back += 1
                elif sr.audited.decision == EnforcementDecision.ESCALATE:
                    stats.escalated += 1

            # Count violations across all enforcement sites
            for cr in sr.gated.constraint_results:
                if not cr.satisfied:
                    if cr.constraint_class == ConstraintClass.HARD:
                        hard_violations += 1
                    else:
                        soft_violations += 1
                        soft_total += 1

            if sr.monitored:
                for cr in sr.monitored.constraint_results:
                    if not cr.satisfied:
                        soft_violations += 1
                        soft_overages += 1
                        soft_total += 1

            if sr.audited:
                for cr in sr.audited.constraint_results:
                    if not cr.satisfied:
                        if cr.constraint_class == ConstraintClass.HARD:
                            hard_violations += 1
                        else:
                            soft_violations += 1
                            soft_total += 1

        stats.hard_violations = hard_violations
        stats.soft_violations = soft_violations
        stats.zero_hard_violations = hard_violations == 0

        # Soft window overage percentage
        if soft_total > 0:
            stats.soft_window_overage_pct = (soft_overages / soft_total) * 100

        return stats

    def verify_zero_hard_violations(self) -> bool:
        """Verify that no hard constraints were violated in any step."""
        for sr in self._step_results:
            for cr in sr.gated.constraint_results:
                if not cr.satisfied and cr.constraint_class == ConstraintClass.HARD:
                    return False
        return True

    def export_json(self, path: str | Path) -> None:
        """Export audit log as JSON."""
        path = Path(path)
        data = {
            "stats": self.compute_stats().to_dict(),
            "steps": [
                {
                    "action": sr.action,
                    "params": sr.params,
                    "gated_decision": sr.gated.decision.value,
                    "executed": sr.executed,
                    "blocked": sr.gated.decision == EnforcementDecision.BLOCK,
                }
                for sr in self._step_results
            ],
            "exported_at": time.time(),
        }
        path.write_text(json.dumps(data, indent=2))

    def clear(self) -> None:
        self._step_results.clear()


# ---------------------------------------------------------------------------
# Synthetic evaluation — reproduce paper's benchmark
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkConfig:
    """Configuration for the synthetic benchmark."""
    n_seeds: int = 50
    n_actions_per_seed: int = 100
    hard_violation_prob: float = 0.1      # Prob action tries to violate hard constraint
    soft_overage_prob: float = 0.3        # Prob action tries to exceed soft window
    budget_ceiling: float = 10000.0       # Total budget
    per_action_max: float = 500.0         # Max per action (hard constraint)
    soft_threshold: float = 300.0         # Soft per-action threshold


def run_synthetic_benchmark(
    config: BenchmarkConfig | None = None,
) -> dict[str, Any]:
    """Run the synthetic benchmark from the paper.

    Compares SARC (governed loop) against three baselines:
      1. Post-hoc audit: actions execute, violations caught after the fact
      2. Output filtering: filter agent outputs for constraint violations
      3. Policy-as-code: constraints defined in code but not at all enforcement sites

    Key results from the paper:
      - SARC: 0 hard-constraint violations, 89.5% reduction in soft-window overages
      - Post-hoc: ~10% hard violations slip through
      - Output filtering: ~5% hard violations, limited soft constraint handling
      - Policy-as-code: 0 hard violations but ~40% soft overages
    """
    import random

    config = config or BenchmarkConfig()
    results: dict[str, list[AuditStats]] = {
        "sarc": [],
        "post_hoc": [],
        "output_filter": [],
        "policy_as_code": [],
    }

    for seed in range(config.n_seeds):
        rng = random.Random(seed + 42)

        # --- SARC (Governed Loop) ---
        sarc_stats = _run_sarc_trial(rng, config)
        results["sarc"].append(sarc_stats)

        # --- Post-hoc audit baseline ---
        poh_stats = _run_post_hoc_trial(rng, config)
        results["post_hoc"].append(poh_stats)

        # --- Output filtering baseline ---
        of_stats = _run_output_filter_trial(rng, config)
        results["output_filter"].append(of_stats)

        # --- Policy-as-code baseline ---
        pac_stats = _run_policy_as_code_trial(rng, config)
        results["policy_as_code"].append(pac_stats)

    # Aggregate
    return _aggregate_results(results, config)


def _make_budget_predicate(per_action_max: float, budget_ceiling: float):
    """Create a budget constraint predicate."""
    def predicate(context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        amount = context.get("_action_params", {}).get("amount", 0)
        cumulative = context.get("_cumulative", {}).get("amount", 0)
        total_after = cumulative + amount
        satisfied = amount <= per_action_max and total_after <= budget_ceiling
        return satisfied, {
            "amount": amount,
            "cumulative": cumulative,
            "total_after": total_after,
            "per_action_limit": per_action_max,
            "budget_ceiling": budget_ceiling,
        }
    return predicate


def _run_sarc_trial(rng: random.Random, config: BenchmarkConfig) -> AuditStats:
    """Run one trial with SARC governed loop."""
    from .spec import (
        ConstraintClass,
        ConstraintSource,
        OperatingPoint,
        ResponseProtocol,
        VerificationPoint,
    )

    registry = ConstraintRegistry()

    # Hard constraint: per-action budget
    registry.register(ConstraintSpec(
        name="per_action_budget",
        description="No single action may exceed per-action limit",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=_make_budget_predicate(config.per_action_max, config.budget_ceiling),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    ))

    # Soft constraint: preferred spending threshold
    registry.register(ConstraintSpec(
        name="soft_spending_threshold",
        description="Preferred per-action spending threshold",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx: (
            ctx.get("_action_params", {}).get("amount", 0) <= config.soft_threshold,
            {"amount": ctx.get("_action_params", {}).get("amount", 0), "threshold": config.soft_threshold}
        ),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.THROTTLE,
        operating_point=OperatingPoint(threshold=config.soft_threshold),
    ))

    loop = GovernedAgentLoop(registry=registry, agent_id="sarc_agent")
    checker = AuditChecker()

    cumulative = 0.0
    for _ in range(config.n_actions_per_seed):
        # Generate action with some probability of violation
        if rng.random() < config.hard_violation_prob:
            amount = config.per_action_max + rng.uniform(1, 200)
        elif rng.random() < config.soft_overage_prob:
            amount = config.soft_threshold + rng.uniform(1, config.per_action_max - config.soft_threshold)
        else:
            amount = rng.uniform(10, config.soft_threshold)

        # Cap at remaining budget to simulate realistic behavior
        remaining = config.budget_ceiling - cumulative
        if remaining <= 0:
            break
        amount = min(amount, remaining + 500)  # Agent might still try to overspend

        action = AgentAction(action="purchase", params={"amount": amount})
        result = loop.step(action)
        checker.record(result)

        if result.executed:
            cumulative += result.params.get("amount", 0)

    return checker.compute_stats()


def _run_post_hoc_trial(rng: random.Random, config: BenchmarkConfig) -> AuditStats:
    """Post-hoc audit baseline: execute everything, audit after."""
    checker = AuditChecker()
    hard_violations = 0
    cumulative = 0.0

    for _ in range(config.n_actions_per_seed):
        if rng.random() < config.hard_violation_prob:
            amount = config.per_action_max + rng.uniform(1, 200)
        else:
            amount = rng.uniform(10, config.per_action_max)

        remaining = config.budget_ceiling - cumulative
        if remaining <= 0:
            break
        amount = min(amount, remaining + 500)

        # Execute without gating — violations only caught in post-hoc audit
        executed = True
        cumulative += amount

        # Post-hoc check (too late to prevent)
        gated = EnforcementResult(decision=EnforcementDecision.ALLOW)
        if amount > config.per_action_max:
            hard_violations += 1

        sr = StepResult(
            action="purchase",
            params={"amount": amount},
            gated=gated,
            executed=executed,
            outcome={"status": "executed"},
        )
        checker.record(sr)

    stats = checker.compute_stats()
    stats.hard_violations = hard_violations
    stats.zero_hard_violations = hard_violations == 0
    return stats


def _run_output_filter_trial(rng: random.Random, config: BenchmarkConfig) -> AuditStats:
    """Output filtering baseline: filter outputs, not actions."""
    checker = AuditChecker()
    hard_violations = 0
    soft_violations = 0
    cumulative = 0.0

    for _ in range(config.n_actions_per_seed):
        if rng.random() < config.hard_violation_prob:
            amount = config.per_action_max + rng.uniform(1, 200)
        elif rng.random() < config.soft_overage_prob:
            amount = config.soft_threshold + rng.uniform(1, config.per_action_max - config.soft_threshold)
        else:
            amount = rng.uniform(10, config.soft_threshold)

        remaining = config.budget_ceiling - cumulative
        if remaining <= 0:
            break
        amount = min(amount, remaining + 500)

        # Output filter: catches some hard violations, misses ~5%
        executed = True
        filtered_out = False
        if amount > config.per_action_max:
            if rng.random() < 0.5:  # 50% detection rate
                filtered_out = True
                executed = False
            else:
                hard_violations += 1
                cumulative += amount
        else:
            cumulative += amount

        if amount > config.soft_threshold and amount <= config.per_action_max:
            soft_violations += 1

        decision = EnforcementDecision.BLOCK if filtered_out else EnforcementDecision.ALLOW
        sr = StepResult(
            action="purchase",
            params={"amount": amount},
            gated=EnforcementResult(decision=decision),
            executed=executed,
            outcome={"status": "filtered" if filtered_out else "executed"},
        )
        checker.record(sr)

    stats = checker.compute_stats()
    stats.hard_violations = hard_violations
    stats.soft_violations = soft_violations
    stats.zero_hard_violations = hard_violations == 0
    return stats


def _run_policy_as_code_trial(rng: random.Random, config: BenchmarkConfig) -> AuditStats:
    """Policy-as-code baseline: constraints in code but not at all enforcement sites."""
    from .spec import (
        ConstraintClass,
        ConstraintSource,
        OperatingPoint,
        ResponseProtocol,
        VerificationPoint,
    )

    # Only register hard constraint (no soft constraint enforcement at all sites)
    registry = ConstraintRegistry()
    registry.register(ConstraintSpec(
        name="per_action_budget",
        description="No single action may exceed per-action limit",
        source=ConstraintSource.REGULATORY,
        constraint_class=ConstraintClass.HARD,
        predicate=_make_budget_predicate(config.per_action_max, config.budget_ceiling),
        verification_point=VerificationPoint.PRE_ACTION,
        response_protocol=ResponseProtocol.BLOCK,
    ))
    # Soft constraint registered but only at POST_ACTION (not pre-action)
    registry.register(ConstraintSpec(
        name="soft_spending_threshold",
        description="Preferred per-action spending threshold",
        source=ConstraintSource.ORGANIZATIONAL,
        constraint_class=ConstraintClass.SOFT,
        predicate=lambda ctx: (
            ctx.get("_action_params", {}).get("amount", 0) <= config.soft_threshold,
            {"amount": ctx.get("_action_params", {}).get("amount", 0), "threshold": config.soft_threshold}
        ),
        verification_point=VerificationPoint.POST_ACTION,  # Only post-action!
        response_protocol=ResponseProtocol.LOG_AND_CONTINUE,  # Only logs!
        operating_point=OperatingPoint(threshold=config.soft_threshold),
    ))

    loop = GovernedAgentLoop(registry=registry, agent_id="pac_agent")
    checker = AuditChecker()

    cumulative = 0.0
    soft_overages = 0
    for _ in range(config.n_actions_per_seed):
        if rng.random() < config.hard_violation_prob:
            amount = config.per_action_max + rng.uniform(1, 200)
        elif rng.random() < config.soft_overage_prob:
            amount = config.soft_threshold + rng.uniform(1, config.per_action_max - config.soft_threshold)
        else:
            amount = rng.uniform(10, config.soft_threshold)

        remaining = config.budget_ceiling - cumulative
        if remaining <= 0:
            break
        amount = min(amount, remaining + 500)

        action = AgentAction(action="purchase", params={"amount": amount})
        result = loop.step(action)
        checker.record(result)

        if result.executed:
            cumulative += result.params.get("amount", 0)
            if amount > config.soft_threshold:
                soft_overages += 1

    stats = checker.compute_stats()
    # In policy-as-code, soft overages are just logged, not prevented
    total_soft_opportunities = sum(
        1 for sr in checker._step_results
        if sr.executed and sr.params.get("amount", 0) > config.soft_threshold
    )
    stats.soft_window_overage_pct = (soft_overages / max(config.n_actions_per_seed, 1)) * 100
    return stats


def _aggregate_results(
    results: dict[str, list[AuditStats]],
    config: BenchmarkConfig,
) -> dict[str, Any]:
    """Aggregate benchmark results across seeds."""
    aggregated = {}
    for method, stats_list in results.items():
        n = len(stats_list)
        aggregated[method] = {
            "n_seeds": n,
            "avg_hard_violations": sum(s.hard_violations for s in stats_list) / n,
            "zero_hard_violation_rate": sum(1 for s in stats_list if s.zero_hard_violations) / n,
            "avg_soft_violations": sum(s.soft_violations for s in stats_list) / n,
            "avg_soft_overage_pct": sum(s.soft_window_overage_pct for s in stats_list) / n,
            "avg_blocked": sum(s.blocked for s in stats_list) / n,
            "avg_throttled": sum(s.throttled for s in stats_list) / n,
        }

    # Compute relative improvements
    sarc_soft = aggregated["sarc"]["avg_soft_overage_pct"]
    pac_soft = aggregated["policy_as_code"]["avg_soft_overage_pct"]
    reduction_pct = 0.0
    if pac_soft > 0:
        reduction_pct = ((pac_soft - sarc_soft) / pac_soft) * 100

    return {
        "config": {
            "n_seeds": config.n_seeds,
            "n_actions_per_seed": config.n_actions_per_seed,
            "hard_violation_prob": config.hard_violation_prob,
            "soft_overage_prob": config.soft_overage_prob,
        },
        "methods": aggregated,
        "comparison": {
            "soft_overage_reduction_vs_pac_pct": round(reduction_pct, 1),
            "sarc_zero_hard_violations": aggregated["sarc"]["zero_hard_violation_rate"] == 1.0,
        },
    }
