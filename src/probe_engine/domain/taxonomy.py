"""Taxonomy coordinates and manual control overrides (spec §5.1, §5.4)."""

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import EvidenceType, TaxonomySystem


class TaxonomyTag(StrictModel):
    """A single taxonomy coordinate carried by a probe (extraction, not interpretation)."""

    system: TaxonomySystem
    id: str
    name: str | None = None


class ControlOverride(StrictModel):
    """Explicit control attachment for probes lacking a clean public coordinate (§5.4)."""

    framework: str
    control_id: str
    rationale: str
    evidence_type: EvidenceType = EvidenceType.BEHAVIORAL
    override: bool = True
