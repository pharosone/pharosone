"""Universality gating: the authz probe selects for ANY agent that declares an account_modify
capability AND an acting principal — and gates OUT (blind spot, never a silent pass) when either is
missing, including the example-agent profile (no authz surface)."""

from pathlib import Path

from probe_engine.config.profile import load_profile, run_config_from_profile
from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import Principal, RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run.selection import select_probes

_ROOT = Path(__file__).parents[2]
CORPUS = "corpus/probes"
_MODIFY = ToolSpec(name="reset_password", capabilities=["account_modify"],
                   authz_action="modify", resource_arg="account_id", dangerous=True)
_PID = "authz-cross-owner-modify"


def _selected(*, principal, tools):
    rc = RunConfig(
        target=TargetConfig(tier="bridge", acting_principal=principal),
        tool_inventory=list(tools),
        available_tools=[t.name for t in tools],
        n_variants=1, epochs=1,
        thresholds=Thresholds(), run_id="r", timestamp="t",
    )
    return {p.id for p in select_probes(load_corpus(CORPUS), rc)}


def test_selects_with_capability_and_identity():
    assert _PID in _selected(principal=Principal(id="U_alice"), tools=(_MODIFY,))


def test_gated_out_without_identity_context():
    # capability present but NO acting principal -> identity gate skips it (not a silent pass)
    assert _PID not in _selected(principal=None, tools=(_MODIFY,))


def test_gated_out_without_capability():
    # acting principal present but no account_modify tool -> capability gate skips it
    plain = ToolSpec(name="set_status", capabilities=["status_change"])
    assert _PID not in _selected(principal=Principal(id="U_alice"), tools=(plain,))


def test_example_agent_gates_out_of_authz():
    # the onboarded example profile declares no acting_principal and no account_modify tool ->
    # authz probe must NOT select (blind-spot parity with mcptox-unauthorized-payment).
    rc = run_config_from_profile(
        load_profile(_ROOT / "configs" / "profiles" / "example-agent.yaml"), "r", "t"
    )
    sel = {p.id for p in select_probes(load_corpus(CORPUS), rc)}
    assert _PID not in sel


def test_universality_same_probe_selects_for_two_different_agents():
    # the load-bearing universality proof: ZERO engine edits — two unrelated agents (banking,
    # healthcare) both onboard the SAME universal authz probe purely via profile capability+identity
    # mapping. Both example profiles must VALIDATE and select it.
    for prof in ("bank-reset.yaml", "health-records.yaml"):
        rc = run_config_from_profile(
            load_profile(_ROOT / "configs" / "profiles" / prof), "r", "t"
        )
        sel = {p.id for p in select_probes(load_corpus(CORPUS), rc)}
        assert _PID in sel, f"{prof} should select the universal authz probe"
