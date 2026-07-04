"""Report data model (spec §7).

The report carries three complementary machine-readable views of one run:

* ``coverage`` — the DENSITY / testability dimension per control (was the control exercised by
  enough distinct probes?), computed by :func:`mapping.coverage.compute_coverage`.
* ``controls`` — the AUDIT dimension per control (did the agent RESIST?), added here for the
  PharosOne cabinet: a ``ControlVerdict`` + the probes that support it + whether it is
  ``auto_closeable``. This is the contract the cabinet reads to auto-close covered AIUC-1 controls.
* ``findings`` — one stats-only record per evaluated probe, with the crosswalk-resolved AIUC-1
  controls and taxonomy coordinates already attached, so a downstream consumer never re-runs the
  mapping. Findings NEVER carry a transcript, protected reference, or oracle patterns.
"""

from enum import Enum

from probe_engine.domain.base import StrictModel
from probe_engine.domain.coverage import Coverage
from probe_engine.domain.enums import CoverageStatus, EvidenceStatus, EvidenceType, Severity
from probe_engine.domain.evidence import Evidence


class ControlVerdict(str, Enum):
    """Per-control AUDIT verdict — the pass/fail dimension the PharosOne cabinet reads to auto-close
    controls, DISTINCT from ``CoverageStatus`` (the density/testability dimension).

    A control is ``auto_closeable`` ONLY when the verdict is ``PASSED``; every other value keeps the
    control OPEN (invariant: blind spots and unverified verdicts are never silent passes)."""

    PASSED = "passed"                                # covered, density met, every contributing probe passed
    FAILED = "failed"                                # >=1 contributing probe demonstrated a vulnerability (fail)
    UNVERIFIED = "unverified"                        # >=1 contributing probe needs judge adjudication; not a pass
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # probes lacked the statistical power to confirm robustness
    PARTIAL = "partial"                              # all contributing probes passed but density below threshold
    NOT_TESTED = "not_tested"                        # behaviorally testable, but no probe reached it (coverage gap)
    NOT_TESTABLE = "not_testable"                    # requires configuration / documentation / telemetry evidence


class SupportingProbe(StrictModel):
    """One probe's contribution to a control's audit verdict (stats-only, no transcript)."""

    probe_id: str
    status: EvidenceStatus
    asr: float
    evidence_type: EvidenceType
    via_override: bool = False


class ControlAudit(StrictModel):
    """Per-control AIUC-1 audit record — the machine-readable unit the cabinet uses to auto-close a
    control. ``title`` is taken verbatim from ``frameworks/aiuc-1.yaml`` (never invented)."""

    control_id: str
    category: str
    title: str
    behaviorally_testable: bool
    coverage_status: CoverageStatus
    verdict: ControlVerdict
    auto_closeable: bool
    verdict_reason: str
    n_distinct_probes: int
    required_min: int | None
    density_met: bool
    aggregate_asr: float | None
    evidence_types: list[EvidenceType]
    supporting_probes: list[SupportingProbe]


class FindingItem(StrictModel):
    """A stats-only per-probe finding for the cabinet/portal.

    Structurally free of any transcript, protected reference, or oracle-pattern data — it carries
    only aggregate statistics plus the crosswalk-resolved AIUC-1 controls and taxonomy coordinates.
    """

    probe_id: str
    severity: Severity
    source: str
    scenario: str
    n_turns: int
    status: EvidenceStatus
    fired: bool                 # True when the attack succeeded at least once (n_success > 0)
    asr: float
    ci_low: float
    ci_high: float
    wilson_ci: list[float]      # [ci_low, ci_high] — the Wilson score interval, for convenience
    n_trials: int
    n_success: int
    n_errors: int
    power: float | None
    early_stopped: bool
    taxonomy: list[dict]        # [{"system": ..., "id": ..., "name": ...}], sorted
    mapped_controls: list[str]  # AIUC-1 control ids this finding maps to (framework-filtered, sorted)


class GapItem(StrictModel):
    control_id: str
    title: str
    status: CoverageStatus
    n_distinct_probes: int
    required_min: int | None
    behaviorally_testable: bool


class Report(StrictModel):
    scope: dict
    coverage: list[Coverage]
    evidence: list[Evidence]
    gaps: list[GapItem]
    aggregates: dict
    # Cabinet-facing views (additive, always present). `controls` is the per-control AIUC-1 audit
    # verdict + supporting probes; `findings` is the stats-only per-probe record with resolved
    # controls/taxonomy. Both are deterministic (stable ordering) and secret-free by construction.
    controls: list[ControlAudit] = []
    findings: list[FindingItem] = []
    # B2: ids of probes SKIPPED because the target can't adjudicate their oracle (blind spots).
    # Surfaced in the audit artifact so a skip is never invisible — never a silent pass (invariant 3).
    # Defaults to [] and is dropped from the JSON when empty (byte-compatible with before).
    blind_spots: list[str] = []
    # DELIBERATE scope reductions: attack approaches (scenario families) the operator chose NOT to
    # run, and the probe ids dropped for that reason. NOT blind spots, NOT passes — disclosed so a
    # narrowed run never reads as robust against an approach it never exercised. Empty by default.
    excluded_approaches: list[str] = []
    scope_excluded_probes: list[str] = []
    # Audit reproducibility (spec): the trial-allocation plan (strategy + model + per-probe
    # n_variants/epochs) and the LLM-synthesis outcome (accepted ids + rejected triage reasons),
    # recorded verbatim as `AllocationPlan.as_dict()` / `SynthesisResult.as_dict()`. Both default
    # None so a report built without a planner/synthesis is byte-compatible with before.
    plan: dict | None = None
    synthesis: dict | None = None
