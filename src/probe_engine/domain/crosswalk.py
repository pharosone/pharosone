"""Crosswalk: taxonomy coordinate -> control(s), versioned and pinned (spec §5)."""

from pydantic import Field

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import EvidenceType, TaxonomySystem


class CrosswalkControlRef(StrictModel):
    control_id: str
    evidence_type: EvidenceType = EvidenceType.BEHAVIORAL


class Mapping(StrictModel):
    """One crosswalk row: a taxonomy coordinate and the controls it credits."""

    taxonomy_system: TaxonomySystem
    taxonomy_id: str
    controls: list[CrosswalkControlRef]
    verified_by: str = "human"
    notes: str | None = None


class Crosswalk(StrictModel):
    framework: str
    framework_version: str
    taxonomy_version: dict[str, str] = Field(default_factory=dict)
    entries: list[Mapping]

    def lookup(
        self, system: TaxonomySystem, taxonomy_id: str
    ) -> list[CrosswalkControlRef]:
        """Exact-coordinate lookup (no prefix matching — preserves granularity §5.4)."""
        for entry in self.entries:
            if entry.taxonomy_system == system and entry.taxonomy_id == taxonomy_id:
                return entry.controls
        return []
