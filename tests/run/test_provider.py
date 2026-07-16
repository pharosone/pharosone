from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.run.executor import _eval_model


def _rc(**target) -> RunConfig:
    return RunConfig(target=TargetConfig(**target), thresholds=Thresholds(), run_id="r", timestamp="t")


def test_openrouter_prefixes_bare_slug():
    tc = TargetConfig(tier="model", provider="openrouter", model="anthropic/claude-3.5-sonnet")
    assert tc.resolved_model() == "openrouter/anthropic/claude-3.5-sonnet"


def test_no_double_prefix():
    tc = TargetConfig(tier="model", provider="openrouter", model="openrouter/openai/gpt-4o")
    assert tc.resolved_model() == "openrouter/openai/gpt-4o"


def test_no_provider_is_passthrough():
    tc = TargetConfig(tier="model", model="anthropic/claude-opus-4-8")
    assert tc.resolved_model() == "anthropic/claude-opus-4-8"


def test_eval_model_resolves_openrouter():
    rc = _rc(tier="model", provider="openrouter", model="anthropic/claude-3.5-sonnet")
    assert _eval_model(rc) == "openrouter/anthropic/claude-3.5-sonnet"


def test_attacker_model_resolution_with_provider():
    tc = TargetConfig(tier="model", provider="openrouter", model="x/y")
    assert tc.resolved_attacker_model() == "openrouter/x/y"        # defaults to model
    tc2 = TargetConfig(tier="model", provider="openrouter", model="x/y", attacker_model="z/w")
    assert tc2.resolved_attacker_model() == "openrouter/z/w"


def test_local_paraphraser_survives_hosted_provider():
    # A local hf/ paraphraser under an OpenRouter target must NOT be rewritten to
    # openrouter/hf/... — this is what keeps variation fully local while the target is hosted.
    tc = TargetConfig(tier="model", provider="openrouter", model="anthropic/claude-3.5-sonnet",
                      paraphrase_model="hf/ibm-granite/granite-4.1-3b")
    assert tc.resolved_model() == "openrouter/anthropic/claude-3.5-sonnet"
    assert tc.resolved_paraphrase_model() == "hf/ibm-granite/granite-4.1-3b"


def test_local_providers_not_prefixed():
    for m in ("hf/x/y", "vllm/x/y", "ollama/granite", "openai-api/local"):
        tc = TargetConfig(tier="model", provider="openrouter", model="a/b", paraphrase_model=m)
        assert tc.resolved_paraphrase_model() == m


def test_bare_org_model_still_prefixed():
    # A bare org/model (not a known provider) still gets the target provider — backward compatible.
    tc = TargetConfig(tier="model", provider="openrouter", model="a/b",
                      paraphrase_model="ibm-granite/granite-4.1-3b")
    assert tc.resolved_paraphrase_model() == "openrouter/ibm-granite/granite-4.1-3b"


def test_local_pharos_judge_survives_hosted_provider():
    # The Local-onboarding judge (openai-api/... is a KNOWN local provider) must NOT be rewritten to
    # openrouter/openai-api/... even under a hosted target — it points at the local llama.cpp endpoint.
    tc = TargetConfig(
        provider="openrouter",
        judge_model="openai-api/pharos-judge/pharos-judge-free",
    )
    assert tc.resolved_judge_model() == "openai-api/pharos-judge/pharos-judge-free"


def test_local_granite_attacker_paraphrase_survive_hosted_provider():
    # The Local attacker/paraphrase Granite strings stay local under a hosted provider too.
    tc = TargetConfig(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        attacker_model="openai-api/pharos-local/granite-4.1-3b",
        paraphrase_model="openai-api/pharos-local/granite-4.1-3b",
    )
    assert tc.resolved_attacker_model() == "openai-api/pharos-local/granite-4.1-3b"
    assert tc.resolved_paraphrase_model() == "openai-api/pharos-local/granite-4.1-3b"
    # a vllm/ Granite 8b (another known local provider) is likewise never double-prefixed
    tc2 = TargetConfig(provider="openrouter", model="a/b",
                       attacker_model="vllm/ibm-granite/granite-4.1-8b")
    assert tc2.resolved_attacker_model() == "vllm/ibm-granite/granite-4.1-8b"
