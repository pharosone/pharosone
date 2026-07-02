"""Run Profile: a reusable YAML bundle describing how to test one agent/industry —
target (tier, model, system prompt), declared tool inventory, industry, depth, thresholds.
Loaded with `--profile` (spec §4, §8 "new industry / new tool")."""

from pathlib import Path

import yaml
from pydantic import Field, ValidationError

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import Severity
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec


class ProfileError(Exception):
    def __init__(self, message: str, path: str | None = None):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


class RunProfile(StrictModel):
    name: str = "profile"
    industry: str = "any"
    target: TargetConfig = Field(default_factory=TargetConfig)
    tools: list[ToolSpec] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    variation_strategy: str = "deterministic"  # "deterministic" | "llm"
    # --- attack planner + probe synthesis (all defaulted -> existing profiles unchanged) ---
    planner: str = "deterministic"  # "deterministic" | "llm" — how trials are allocated across eligible probes
    max_trials: int | None = None   # optional global budget the deterministic/llm planner scales to fit
    synthesize_n: int = 0           # number of new candidate probes the synthesis LLM proposes (0 = off)
    fail_fast: bool = False          # stop a probe's trials early once a FAIL is statistically certain
    judge_batch_size: int = 0        # >0: judge a probe's trials in chunks of this size (small prompts, no hang)
    judge_timeout_s: float = 60.0    # per judge model call timeout (resilient_generate); bounds a stuck call
    # planner_model / synthesis_model live on `target:` (TargetConfig) — default Opus 4.8, provider-aware
    n_variants: int = 5
    epochs: int = 2
    seed: int = 1
    severity_floor: Severity = Severity.INFO
    thresholds: Thresholds = Field(default_factory=Thresholds)
    mock_rule: str = "by_fingerprint"
    mock_threshold: int = 30
    standards: list[str] = Field(default_factory=lambda: ["aiuc-1"])


def load_profile(path: str | Path) -> RunProfile:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid YAML: {exc}", str(path)) from exc
    try:
        return RunProfile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(f"schema error: {exc}", str(path)) from exc


def run_config_from_profile(profile: RunProfile, run_id: str, timestamp: str) -> RunConfig:
    """Derive a RunConfig from a profile. The declared tool inventory also drives selection
    (available_tools = inventory names), so probes needing absent tools are skipped."""
    available_tools = [t.name for t in profile.tools]
    return RunConfig(
        target=profile.target,
        industry=profile.industry,
        available_tools=available_tools,
        tool_inventory=profile.tools,
        standards=profile.standards,
        languages=profile.languages,
        variation_strategy=profile.variation_strategy,
        planner=profile.planner,
        max_trials=profile.max_trials,
        synthesize_n=profile.synthesize_n,
        fail_fast=profile.fail_fast,
        judge_batch_size=profile.judge_batch_size,
        judge_timeout_s=profile.judge_timeout_s,
        n_variants=profile.n_variants,
        epochs=profile.epochs,
        severity_floor=profile.severity_floor,
        corpus_version="seed",
        thresholds=profile.thresholds,
        run_id=run_id,
        timestamp=timestamp,
    )
