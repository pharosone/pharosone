"""Planner / synthesis CONFIG fields on RunConfig + TargetConfig.

These cover only the config surface (defaults, provider prefixing, explicit override,
backward compatibility). The planner/synthesis behavior itself is tested elsewhere.
"""

from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds


def _rc(**target) -> RunConfig:
    return RunConfig(target=TargetConfig(**target), thresholds=Thresholds(), run_id="r", timestamp="t")


# --- RunConfig defaults ------------------------------------------------------

def test_runconfig_planner_defaults_deterministic():
    rc = _rc()
    assert rc.planner == "deterministic"


def test_runconfig_max_trials_defaults_none():
    assert _rc().max_trials is None


def test_runconfig_synthesize_n_defaults_zero():
    assert _rc().synthesize_n == 0


def test_runconfig_new_fields_overridable():
    rc = _rc()
    rc2 = rc.model_copy(update={"planner": "llm", "max_trials": 200, "synthesize_n": 5})
    assert rc2.planner == "llm"
    assert rc2.max_trials == 200
    assert rc2.synthesize_n == 5
    # original untouched (no mutation of corpus/run defaults)
    assert rc.planner == "deterministic" and rc.max_trials is None and rc.synthesize_n == 0


# --- TargetConfig model resolution -------------------------------------------

def test_resolved_planner_model_default_is_opus_4_8():
    assert TargetConfig().resolved_planner_model() == "anthropic/claude-opus-4-8"


def test_resolved_synthesis_model_default_is_opus_4_8():
    assert TargetConfig().resolved_synthesis_model() == "anthropic/claude-opus-4-8"


def test_default_models_are_never_none():
    tc = TargetConfig()
    # both default-resolved models are non-empty strings (typed str, not str|None)
    assert isinstance(tc.resolved_planner_model(), str)
    assert isinstance(tc.resolved_synthesis_model(), str)


def test_openrouter_prefixes_default_planner_model():
    tc = TargetConfig(provider="openrouter")
    assert tc.resolved_planner_model() == "openrouter/anthropic/claude-opus-4-8"


def test_openrouter_prefixes_default_synthesis_model():
    tc = TargetConfig(provider="openrouter")
    assert tc.resolved_synthesis_model() == "openrouter/anthropic/claude-opus-4-8"


def test_openrouter_prefixes_overridden_planner_model():
    tc = TargetConfig(provider="openrouter", planner_model="anthropic/claude-3.5-sonnet")
    assert tc.resolved_planner_model() == "openrouter/anthropic/claude-3.5-sonnet"


def test_explicit_planner_model_override():
    tc = TargetConfig(planner_model="anthropic/claude-3.5-haiku")
    assert tc.resolved_planner_model() == "anthropic/claude-3.5-haiku"


def test_explicit_synthesis_model_override():
    tc = TargetConfig(synthesis_model="anthropic/claude-3.5-haiku")
    assert tc.resolved_synthesis_model() == "anthropic/claude-3.5-haiku"


def test_no_double_prefix_when_already_prefixed():
    tc = TargetConfig(provider="openrouter", planner_model="openrouter/anthropic/claude-opus-4-8")
    assert tc.resolved_planner_model() == "openrouter/anthropic/claude-opus-4-8"


def test_planner_synthesis_models_independent():
    tc = TargetConfig(planner_model="anthropic/claude-3.5-haiku")
    assert tc.resolved_planner_model() == "anthropic/claude-3.5-haiku"
    # synthesis falls back to its own default, unaffected by planner override
    assert tc.resolved_synthesis_model() == "anthropic/claude-opus-4-8"


# --- backward compatibility --------------------------------------------------

def test_existing_kw_construction_still_works():
    # construction sites that never pass the new fields keep working unchanged
    rc = RunConfig(
        target=TargetConfig(tier="model", model="mockllm/model"),
        n_variants=1, epochs=1, thresholds=Thresholds(), run_id="r", timestamp="t",
    )
    assert rc.planner == "deterministic"
    assert rc.synthesize_n == 0
    assert rc.max_trials is None
