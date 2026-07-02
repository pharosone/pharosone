"""Run artifacts: Variant, Trial, and aggregated Evidence (spec §6, §10)."""

from pydantic import Field

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import EvidenceStatus, Severity, VariationStrategy
from probe_engine.domain.probe import Provenance
from probe_engine.domain.taxonomy import ControlOverride, TaxonomyTag


class Trial(StrictModel):
    """One execution of one variant (one Inspect sample x epoch)."""

    variant_id: str
    epoch: int
    success: bool
    oracle_result: dict = Field(default_factory=dict)
    transcript_ref: str | None = None


class Variant(StrictModel):
    """A realized, mutated surface form of a probe (one Inspect dataset sample)."""

    probe_id: str
    variant_id: str
    language: str = "en"
    strategy: VariationStrategy = VariationStrategy.DETERMINISTIC
    mutation_seed: int = 0
    fingerprint: str = ""
    rendered_turns: list[dict] = Field(default_factory=list)


class Evidence(StrictModel):
    """Aggregated result for one probe; carries taxonomy/override for mapping (not controls)."""

    probe_id: str
    severity: Severity
    taxonomy_tags: list[TaxonomyTag]
    control_overrides: list[ControlOverride] = Field(default_factory=list)
    provenance: Provenance
    n_trials: int = 0
    n_success: int = 0
    n_errors: int = 0  # samples the target errored on (e.g. endpoint 5xx) — not counted as trials
    asr: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    power: float | None = None
    status: EvidenceStatus = EvidenceStatus.NOT_RUN
    # fail-fast: True when trials were stopped early because FAIL was statistically certain (the
    # Wilson lower bound ci_low already >= asr_pass). asr is then over the PARTIAL sample run before
    # the stop (ci_low is the proven floor on the true rate); never set on a pass/insufficient run.
    early_stopped: bool = False
    trials: list[Trial] = Field(default_factory=list)
    scenario: str = "single_turn"
    n_turns: int = 1
    transcript: list[dict] = Field(default_factory=list)  # [{role, content}] of one sample (spec §7.3)
