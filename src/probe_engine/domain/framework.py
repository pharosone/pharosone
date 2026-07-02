"""Standard (Framework) and its Controls (spec §6, §12)."""

from pydantic import field_validator, model_validator

from probe_engine.domain.base import StrictModel


class DensityThreshold(StrictModel):
    """Minimum diversity of probes required to consider a control covered (§5.2)."""

    min: int
    max: int

    @model_validator(mode="after")
    def _check_range(self) -> "DensityThreshold":
        if self.min > self.max:
            raise ValueError(f"density min ({self.min}) > max ({self.max})")
        return self


class Verification(StrictModel):
    """Provenance of a control's text, so the report never implies fabricated wording (§5.6)."""

    title: str                    # e.g. "verified@aiuc-1.com"
    wording: str = "unverified"   # "unverified" until checked against authoritative text
    notes: str | None = None


class Control(StrictModel):
    """A single requirement of a standard (e.g. B001)."""

    id: str
    category: str
    title: str
    requirement_text: str | None = None
    mandatory: bool = True
    density_threshold: DensityThreshold | None = None
    behaviorally_testable: bool = True
    verification: Verification | None = None


class Framework(StrictModel):
    """A versioned standard containing controls (e.g. AIUC-1)."""

    id: str
    version: str
    name: str
    controls: list[Control]

    @field_validator("controls")
    @classmethod
    def _unique_ids(cls, controls: list[Control]) -> list[Control]:
        ids = [c.id for c in controls]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate control ids: {sorted(dupes)}")
        return controls

    def get_control(self, control_id: str) -> Control | None:
        return next((c for c in self.controls if c.id == control_id), None)

    def control_ids(self) -> set[str]:
        return {c.id for c in self.controls}
