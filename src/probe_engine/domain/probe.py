"""Probe spec and its nested structures (spec §6)."""

from pydantic import Field, model_validator

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import ScenarioType, Severity, VariationStrategy
from probe_engine.domain.taxonomy import ControlOverride, TaxonomyTag


# Canonical delivery channels (the abstract "doorway" an attack uses to reach the agent). A probe
# names a channel; the agent's profile/adapter maps it to a concrete surface — the same alias
# mechanism as capabilities (probe says `ingested_record`; the adapter routes it to that agent's
# concrete record/card field).
# `message`/`history` are the conversation channel (every target has them, the default). The rest
# are ingestion surfaces a target must explicitly declare (target.channels) to be tested on them —
# an undeclared channel is a blind spot (the probe is skipped, never silently delivered elsewhere).
# `image_content` is the MULTIMODAL ingestion surface: poison text rendered into an image the agent
# ingests (text-in-image / OCR-extracted captions / EXIF metadata / an embedded QR code). The
# concrete pixel rendering is a bridge-adapter responsibility (the adapter's channels() maps it to a
# real image surface); the channel exists in the vocabulary so a probe can declare it and gate
# correctly — undeclared = a blind spot, exactly like the other ingestion channels.
CANONICAL_CHANNELS = frozenset(
    {
        "message",
        "history",
        "ingested_record",
        "retrieved_doc",
        "tool_result",
        "memory",
        "file_content",
        "image_content",
    }
)


class Applicability(StrictModel):
    """Execution parameters that decide whether a probe applies (spec §4)."""

    industries: list[str] = Field(default_factory=lambda: ["any"])
    required_tools: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    models: list[str] = Field(default_factory=lambda: ["any"])
    # An authz probe needs the target to declare an acting principal (identity context). Default
    # False keeps every existing probe's selection unchanged; True = gated out (blind spot, never a
    # silent pass) on a target with no declared principal.
    requires_identity_context: bool = False
    # A state-invariant probe needs the target to declare a lifecycle (seed_stage). Default False;
    # True gates the probe out (blind spot) on a target with no declared lifecycle.
    requires_lifecycle_context: bool = False
    # Stages a lifecycle-sweep replays the SAME attack at (run_probe_lifecycle seeds each in turn).
    # Empty = no sweep. Canonical stage tokens, mapped per-agent via the profile's stage_arg.
    lifecycle_sweep: list[str] = Field(default_factory=list)


class Turn(StrictModel):
    """One step of a scenario; surface text is mutated, structure is the invariant. `channel` is
    the canonical delivery channel for this turn's `poison` (default `message` = the direct
    conversation; e.g. `ingested_record` = poison sitting in a record/card field the agent ingests).

    `multi_channel` makes this a COMBINED / worst-case attack: the turn's `poison` is fanned out
    into EVERY channel the target declares (resolved at run time from `target.channels`, ∪ message),
    each surface carrying a genuinely DIFFERENT variation of the payload (Option B — distinct
    rewrites per channel, so the surfaces don't look identical to a per-channel dedup/filter). This
    is the research-backed "place the payload at every reachable position" ceiling: it tests whether
    the agent holds under simultaneous pressure AND defeats defenses that sanitize only one channel.
    `channel` stays the nominal fallback (default `message`) so the probe still applies — and degrades
    gracefully to a single channel — on a target that declares only the conversation."""

    role: str
    poison: str | None = None
    seed_prompts: list[str] = Field(default_factory=list)
    channel: str = "message"
    multi_channel: bool = False

    @model_validator(mode="after")
    def _valid_channel(self) -> "Turn":
        if self.channel not in CANONICAL_CHANNELS:
            raise ValueError(
                f"turn.channel '{self.channel}' is not canonical; use one of {sorted(CANONICAL_CHANNELS)}"
            )
        return self


class Scenario(StrictModel):
    type: ScenarioType
    turns: list[Turn] = Field(default_factory=list)
    max_turns: int = 6


class VariationConfig(StrictModel):
    strategy: list[VariationStrategy] = Field(
        default_factory=lambda: [VariationStrategy.DETERMINISTIC]
    )
    n_variants: int = 5
    languages: list[str] = Field(default_factory=lambda: ["en"])
    dedup: bool = True


class BinaryCheck(StrictModel):
    oracle: str
    args: dict = Field(default_factory=dict)


class SemanticCheck(StrictModel):
    judge_prompt: str
    confidence_threshold: float = 0.7


class Evaluation(StrictModel):
    binary: BinaryCheck | None = None
    semantic: SemanticCheck | None = None


class SandboxSecret(StrictModel):
    path: str
    content: str


class SandboxSink(StrictModel):
    kind: str
    path: str


class SandboxOracle(StrictModel):
    kind: str
    params: dict = Field(default_factory=dict)


class SandboxSpec(StrictModel):
    seed_secret: SandboxSecret | None = None
    exfil_sink: SandboxSink | None = None
    oracle: SandboxOracle | None = None


class Provenance(StrictModel):
    source: str
    url: str | None = None
    disclosure_id: str | None = None
    harvested_as: str = "corpus_yaml"


class Probe(StrictModel):
    id: str
    version: int = 1
    title: str
    severity: Severity
    intent: str
    taxonomy_tags: list[TaxonomyTag]
    control_overrides: list[ControlOverride] = Field(default_factory=list)
    applicability: Applicability = Field(default_factory=Applicability)
    scenario: Scenario
    variation: VariationConfig = Field(default_factory=VariationConfig)
    evaluation: Evaluation
    sandbox: SandboxSpec | None = None
    expected_behavior: str | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _require_an_evaluation(self) -> "Probe":
        if self.evaluation.binary is None and self.evaluation.semantic is None:
            raise ValueError(f"probe {self.id}: evaluation needs binary and/or semantic")
        return self
