# SARC Benchmark — Synthetic Evaluation

Reproduces the key results from arXiv:2605.07728: zero hard-constraint violations under exact predicates and ~89.5% reduction in soft-window overages vs policy-as-code-only.

## Methodology

We evaluate four governance approaches on a synthetic procurement task:

1. **SARC** — Full four-site enforcement architecture
2. **Post-hoc audit** — Actions execute freely; violations detected after the fact
3. **Output filtering** — Agent outputs filtered for constraint violations (50% detection rate for hard violations)
4. **Policy-as-code** — Constraints defined in code but only enforced at post-action with LOG_AND_CONTINUE

### Setup

| Parameter | Value |
|---|---|
| Seeds | 50 |
| Actions per seed | 100 |
| Hard violation probability | 10% |
| Soft overage probability | 30% |
| Per-action hard limit | $500 |
| Soft preferred threshold | $300 |
| Total budget ceiling | $10,000 |

### Constraints

- **Hard**: `per_action_budget` — No single purchase > $500 (PRE_ACTION, BLOCK)
- **Hard**: `daily_budget_ceiling` — Total spending ≤ $10,000 (PRE_ACTION, BLOCK)
- **Soft**: `soft_spending_threshold` — Preferred per-action ≤ $300 (PRE_ACTION, THROTTLE)
- **Soft**: `vendor_diversity` — No vendor > 60% concentration (POST_ACTION, LOG_AND_CONTINUE)

## Running the Benchmark

```bash
pip install -e .
python -c "from sarc import run_synthetic_benchmark; import json; r = run_synthetic_benchmark(); print(json.dumps(r, indent=2))"
```

Or with custom config:

```python
from sarc import run_synthetic_benchmark, BenchmarkConfig

config = BenchmarkConfig(n_seeds=50, n_actions_per_seed=100)
results = run_synthetic_benchmark(config)
```

## Expected Results

Based on the paper and our reproduction:

| Method | Avg Hard Violations | Zero Hard Violation Rate | Avg Soft Overage % | Soft Overage Reduction vs PaC |
|---|---|---|---|---|
| **SARC** | 0.0 | 100% | ~4.2% | **89.5%** |
| **Post-hoc** | ~10.0 | 0% | ~40% | 0% |
| **Output filter** | ~5.0 | 0% | ~20% | 50% |
| **Policy-as-code** | 0.0 | 100% | ~40% | 0% |

### Key Findings

1. **Zero hard-constraint violations**: SARC's pre-action gate blocks every action that would violate a hard constraint. This is an architectural guarantee, not a probabilistic one — under exact predicates, violations are *structurally impossible*.

2. **89.5% reduction in soft-window overages**: By throttling soft-constraint violations at the pre-action gate (reducing amounts to the operating point threshold), SARC dramatically reduces overages compared to policy-as-code, which only logs violations without preventing them.

3. **Post-hoc is insufficient**: Even with perfect post-action auditing, ~10 hard violations per seed still *execute*. You can detect them, but you can't undo them.

4. **Output filtering is incomplete**: With 50% detection rate (generous), ~5 hard violations per seed still slip through. More importantly, output filtering doesn't prevent the *actions* — just the *responses*.

5. **Policy-as-code without enforcement sites is documentation**: Hard constraints achieve zero violations when coded as pre-action blocks, but soft constraints logged post-action might as well not exist for prevention.

## Why This Matters

The gap between SARC and the baselines isn't incremental — it's architectural:

- **Post-hoc audit** treats governance as observability. You see violations, but can't prevent them.
- **Output filtering** treats governance as censorship. You hide violations, but don't stop them.
- **Policy-as-code** treats governance as documentation. You define constraints, but don't enforce them at the right points.
- **SARC** treats governance as architecture. Constraints are structurally guaranteed to be evaluated at the right enforcement sites, making violations *impossible* rather than *detectable*.

## Reproducibility

All random seeds are deterministic. Running the benchmark multiple times produces identical results:

```python
from sarc import run_synthetic_benchmark

r1 = run_synthetic_benchmark()
r2 = run_synthetic_benchmark()
assert r1 == r2  # Deterministic
```

## Limitations

- **Exact predicates only**: Zero hard-constraint violations requires exact predicate evaluation. Approximate or learned predicates may have non-zero violation rates.
- **Synthetic setting**: Real-world constraints may be more ambiguous or context-dependent.
- **No latency measurement**: The benchmark measures correctness, not performance overhead.
- **Single-task evaluation**: Procurement is one domain; other domains may have different constraint structures.
