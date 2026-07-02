from pathlib import Path

import pytest

from probe_engine.config.profile import load_profile, run_config_from_profile
from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.probe import (
    Applicability,
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.run.executor import _eval_model
from probe_engine.run.selection import select_probes

_ROOT = Path(__file__).resolve().parents[2]


def _probe(pid, industries=None, tools=None, severity="high"):
    return Probe(
        id=pid, title="t", severity=severity, intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        applicability=Applicability(industries=industries or ["any"], required_tools=tools or []),
        scenario=Scenario(type="single_turn", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "x"})),
        provenance=Provenance(source="X"),
    )


def _rc(industry="any", tools=None, tier="mock", model=None, severity_floor="info"):
    return RunConfig(
        target=TargetConfig(tier=tier, model=model),
        industry=industry,
        available_tools=tools or [],
        severity_floor=severity_floor,
        thresholds=Thresholds(),
        run_id="r",
        timestamp="t",
    )


def test_industry_any_runs_all():
    probes = [_probe("u", ["any"]), _probe("fin", ["financial_services"]), _probe("hc", ["healthcare"])]
    assert {p.id for p in select_probes(probes, _rc(industry="any"))} == {"u", "fin", "hc"}


def test_specific_industry_keeps_universal_and_matching():
    probes = [_probe("u", ["any"]), _probe("fin", ["financial_services"]), _probe("hc", ["healthcare"])]
    got = {p.id for p in select_probes(probes, _rc(industry="financial_services"))}
    assert got == {"u", "fin"}


def test_available_tools_restricts_to_subset():
    probes = [_probe("needs_move", tools=["move_file"]), _probe("needs_send", tools=["send_message"])]
    got = {p.id for p in select_probes(probes, _rc(tools=["send_message"]))}
    assert got == {"needs_send"}


def test_empty_tools_means_no_restriction():
    probes = [_probe("needs_move", tools=["move_file"])]
    assert len(select_probes(probes, _rc(tools=[]))) == 1


def test_severity_floor_filters():
    probes = [_probe("lo", severity="low"), _probe("hi", severity="critical")]
    got = {p.id for p in select_probes(probes, _rc(severity_floor="high"))}
    assert got == {"hi"}


def test_eval_model_mock_tier_is_offline():
    assert _eval_model(_rc(tier="mock")) == "mockllm/model"


def test_eval_model_model_tier_uses_target_model():
    assert _eval_model(_rc(tier="model", model="anthropic/claude-opus-4-8")) == "anthropic/claude-opus-4-8"


def test_eval_model_model_tier_requires_a_model():
    with pytest.raises(ValueError):
        _eval_model(_rc(tier="model", model=None))


def test_example_agent_profile_selects_the_capability_and_channel_mapped_probes():
    # the onboarded example profile: its tool->capability map makes the 6 capability-mapped probes
    # select (extractions + the 3 generics via aliases + fake-compliance via send_message), AND
    # because it declares the `ingested_record` channel, the 2 indirect-via-record probes also
    # select. The multi-channel COMBINED probe (status_change, nominal `message` channel) selects
    # too and fans into whatever channels the agent declares. Because the profile ALSO declares the
    # `memory` channel (the agent's `known_answers` block — a confirmed reachable surface), the two
    # universal memory-poison probes (cross-session-leak, rtbf-retention-consistency) select as well:
    # declaring a channel makes the universal probes for it apply. Probes needing capabilities/channels
    # it lacks stay OUT (no false robustness).
    probes = load_corpus(str(_ROOT / "corpus" / "probes"))
    rc = run_config_from_profile(
        load_profile(_ROOT / "configs" / "profiles" / "example-agent.yaml"), "r", "t"
    )
    assert len(probes) == 118
    sel = {p.id for p in select_probes(probes, rc)}
    # The hand-authored (non-garak) corpus selects EXACTLY the capability/channel-mapped set below.
    # Universal garak-* probes also select via the conversation channel; they are checked separately
    # (sanity asserts after the blind-spot block) so this exact pin doesn't churn as the corpus grows.
    assert {s for s in sel if not s.startswith("garak-")} == {
        "extraction-system-prompt",
        "adaptive-escalation-extraction",
        "atb-fake-compliance-impersonation",
        "generic-record-fabrication",
        "generic-status-manipulation",
        "generic-unwarranted-escalation",
        "indirect-status-via-record",   # selected via the declared ingested_record channel
        "indirect-leak-via-record",
        "adaptive-indirect-record",     # adaptive indirect: status_change capability + ingested_record channel
        "multi-channel-status",         # combined worst-case: status_change, fans into all declared channels
        "status-no-regress",            # state-invariant: status_change + declared lifecycle (seed_stage: unknown)
        "cross-session-leak",           # universal memory-poison probe: selected via the declared memory channel
        "rtbf-retention-consistency",   # universal memory-poison probe: selected via the declared memory channel
        # universal (industry=any) probes that gate only on the conversation channel /
        # capabilities this agent has (send_message etc.) — they apply to every agent, so they
        # select here too. Probes needing capabilities/channels the agent lacks still stay OUT.
        "ask-to-translate-and-execute",
        "bot-config-exfiltration",
        "catastrophic-misuse",
        "generative-hallucination",
        "harmful-content-generation",
        "insecure-output-markdown",
        "insecure-output-xss",
        "out-of-scope-action",
        "pii-enumeration",
        "privilege-enumeration",
        "run-the-code-payload",
        "secrets-enumeration",
        "self-roundtrip-base64",
        "token-exhaustion",
    }
    # named blind spots: capabilities the agent has no tool for must not select
    assert "mcptox-unauthorized-payment" not in sel   # needs create_payment_mandate/schedule_transfer
    assert "mcptox-ssh-key-exfil" not in sel           # needs a read_file/move_file leak tool
    # universal garak-* probes (industry=any, conversation channel) select here too...
    assert {"garak-dan", "garak-sysprompt_extraction", "garak-web_injection-markdown-image"} <= sel
    # ...but garak probes needing capabilities/channels this profile lacks stay OUT (no false robustness)
    assert "garak-malwaregen" not in sel              # needs code_exec
    assert "garak-latentinjection-resume" not in sel  # needs file_content / retrieved_doc
