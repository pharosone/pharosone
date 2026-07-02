"""LLM attack-planner: budget allocation + probe synthesis (foundation layer).

This package's FOUNDATION is the planner data structures (`models`) and the scoreable-primitive
inventory (`library_spec`). The allocate/synthesize executors live in `plan.allocate` /
`plan.synthesize` and are imported BY FULL PATH (not re-exported here) to avoid import cycles and
edit contention — they may not exist yet.

Re-exports the plain dataclasses so callers can `from probe_engine.plan import AllocationPlan` etc.
"""

from probe_engine.plan.models import (
    AllocationBudget,
    AllocationPlan,
    ProbeAllocation,
    RejectedCandidate,
    SynthesisResult,
)

__all__ = [
    "AllocationBudget",
    "AllocationPlan",
    "ProbeAllocation",
    "RejectedCandidate",
    "SynthesisResult",
]
