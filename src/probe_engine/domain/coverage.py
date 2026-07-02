"""Computed coverage of a control by Evidence (spec §5.2, §5.3, §6)."""

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import CoverageStatus, EvidenceStatus, EvidenceType
from probe_engine.domain.framework import DensityThreshold


class ControlContribution(StrictModel):
    """One probe's evidence reaching a control via crosswalk or override."""

    probe_id: str
    asr: float
    status: EvidenceStatus
    evidence_type: EvidenceType
    via_override: bool = False


class Coverage(StrictModel):
    control_id: str
    framework: str
    category: str
    title: str
    behaviorally_testable: bool
    density_threshold: DensityThreshold | None
    n_distinct_probes: int
    density_met: bool
    aggregate_asr: float | None
    status: CoverageStatus
    evidence_types: list[EvidenceType]
    contributions: list[ControlContribution]
