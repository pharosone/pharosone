"""Foundation data structures for the LLM attack-planner (allocation + synthesis).

These dataclasses are deliberately PLAIN (not StrictModel/pydantic) — they are in-process
plumbing carried through `allocate -> run_corpus` and `synthesize -> validate -> report`, not
parsed from untrusted YAML. They expose `as_dict()` so the report layer can record the exact
plan + synthesis outcome for AUDIT REPRODUCIBILITY (model + seed + per-probe allocation, and the
accepted ids + rejected reasons triage queue).

INVARIANT (deterministic gating is the floor): an `AllocationPlan` re-weights/orders WITHIN the
eligible set — it never drops an eligible probe. Every eligible probe has its own
`ProbeAllocation` with `n_variants >= min_variants` and `epochs >= min_epochs`. The plan does not
enforce that here (the floor is the allocator's job, asserted by its tests); these structures only
carry and faithfully serialize the result.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the heavy domain model at module import time
    from probe_engine.domain.probe import Probe


@dataclass
class ProbeAllocation:
    """How much budget one eligible probe receives in a run.

    `priority` orders execution (higher runs first); `rationale` is a short human-readable note
    explaining the weighting (recorded in the report for audit)."""

    probe_id: str
    n_variants: int
    epochs: int
    priority: int = 0
    rationale: str = ""

    def as_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "n_variants": self.n_variants,
            "epochs": self.epochs,
            "priority": self.priority,
            "rationale": self.rationale,
        }


@dataclass
class AllocationPlan:
    """The full per-probe budget allocation for a run.

    `strategy` is "deterministic" or "llm"; `model` is the resolved planner model id (None offline);
    `total_trials` is the planned sum of n_variants*epochs across items; `notes` carries the offline
    fallback reason or any planner commentary."""

    items: list[ProbeAllocation]
    strategy: str
    model: str | None = None
    total_trials: int = 0
    notes: str = ""

    def for_probe(self, pid: str) -> "ProbeAllocation | None":
        """The allocation for probe `pid`, or None if it has none (the executor then uses
        run-config defaults — never silently skips)."""
        for item in self.items:
            if item.probe_id == pid:
                return item
        return None

    def as_dict(self) -> dict:
        """JSON-serializable plan for the report (per-probe allocation + strategy + model + seed
        provenance)."""
        return {
            "strategy": self.strategy,
            "model": self.model,
            "total_trials": self.total_trials,
            "notes": self.notes,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass
class AllocationBudget:
    """The budget envelope the allocator must respect.

    `default_variants`/`default_epochs` are the uniform baseline; `min_variants`/`min_epochs` are
    the deterministic FLOOR every eligible probe is guaranteed (so a probe is never starved to 0).
    `max_trials` (when set) caps the planned total — the allocator scales down toward the floor,
    never below it."""

    max_trials: int | None
    default_variants: int
    default_epochs: int
    min_variants: int = 1
    min_epochs: int = 1


@dataclass
class RejectedCandidate:
    """An LLM-proposed probe that FAILED the deterministic `validate_candidate` gate. Goes to the
    triage queue (`reasons` lists every failed check) and is NEVER run."""

    raw: dict
    reasons: list[str]

    def as_dict(self) -> dict:
        return {"raw": self.raw, "reasons": list(self.reasons)}


@dataclass
class SynthesisResult:
    """Outcome of an LLM synthesis pass: probes that PARSED + passed the gate (`accepted`) and the
    triage queue (`rejected`). `model` is the resolved synthesis model id (None offline); `notes`
    carries the offline fallback reason."""

    accepted: "list[Probe]" = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)
    model: str | None = None
    notes: str = ""

    def as_dict(self) -> dict:
        """For the report: accepted probe ids + the rejected triage reasons (reproducibility +
        audit). The accepted probes are referenced by id only here — their full spec is persisted
        separately (cli --save-generated)."""
        return {
            "model": self.model,
            "notes": self.notes,
            "accepted_ids": [p.id for p in self.accepted],
            "rejected": [r.as_dict() for r in self.rejected],
        }
