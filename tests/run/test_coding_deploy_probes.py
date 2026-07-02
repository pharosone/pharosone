"""Regression for the coding/deploy-agent threat pack (deploy / CI-tamper / dependency-backdoor /
indirect-via-ticket / cross-session-memory). The pack targets the canonical `deploy`, `edit_file`
and `code_exec` capabilities and the `code_pattern` / `tool_called` oracles. It must:

  * SELECT on a profile that declares the deploy/code_exec/edit_file tools AND the
    ingested_record + memory delivery channels;
  * FIRE end-to-end on the offline mock tier (asr == 1.0, no errors);
  * be correctly SKIPPED as a blind spot when a required ingestion channel (ingested_record /
    memory) is NOT declared — never silently re-routed and read as robust;
  * gate OUT of a deploy probe when the `deploy` capability is not provided.

Fully offline (mock tier, no network/keys).
"""

from pathlib import Path

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run.executor import run_probe
from probe_engine.run.selection import select_probes
from probe_engine.targets.mock import MockPolicy

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _ROOT / "corpus" / "probes"

# direct probes: ride the conversation channel; require deploy/edit_file/code_exec capability only.
_DEPLOY_DIRECT = "unauthorized-deploy"
_DEPLOY_UNREVIEWED = "deploy-unreviewed-code"
_CI_TAMPER = "ci-config-tamper"
_DEP_BACKDOOR = "dependency-backdoor"
_DIRECT = [_DEPLOY_DIRECT, _DEPLOY_UNREVIEWED, _CI_TAMPER, _DEP_BACKDOOR]

# indirect probes: poison rides an ingestion channel that the target must DECLARE.
_INDIRECT_DEPLOY = "indirect-deploy-via-ticket"          # channel ingested_record
_INDIRECT_CODE = "indirect-code-backdoor-via-ticket"     # channel ingested_record
_XSESSION = "cross-session-memory-campaign"              # channel memory
_CHANNEL_GATED = [_INDIRECT_DEPLOY, _INDIRECT_CODE, _XSESSION]

_ALL = [*_DIRECT, *_CHANNEL_GATED]


def _by_id():
    return {p.id: p for p in load_corpus(str(_CORPUS))}


def _inventory():
    # concrete tools mapped to the canonical capabilities the pack targets.
    return [
        ToolSpec(name="ship", capabilities=["deploy"], dangerous=True),
        ToolSpec(name="run_cmd", capabilities=["code_exec"], dangerous=True),
        ToolSpec(name="patch", capabilities=["edit_file"], dangerous=False),
    ]


def _rc(channels=None, inventory=None) -> RunConfig:
    return RunConfig(
        target=TargetConfig(tier="mock", channels=channels or ["message", "history"]),
        tool_inventory=_inventory() if inventory is None else inventory,
        n_variants=2, epochs=1,
        thresholds=Thresholds(), run_id="r", timestamp="2026-06-25T00:00:00Z",
    )


def test_all_present_and_well_formed():
    probes = _by_id()
    for pid in _ALL:
        assert pid in probes, f"{pid} missing from corpus"
        p = probes[pid]
        assert p.severity == "critical"
        assert p.evaluation.binary is not None
        assert p.evaluation.semantic is not None  # judge backstop present


def test_all_select_on_declaring_profile():
    # a target declaring the deploy/code_exec/edit_file tools AND both ingestion channels keeps
    # every probe in the pack (none gated out -> none read as a silent pass).
    rc = _rc(channels=["message", "history", "ingested_record", "memory"])
    selected = {p.id for p in select_probes(list(_by_id().values()), rc)}
    assert set(_ALL).issubset(selected), f"missing: {set(_ALL) - selected}"


def test_all_fire_on_mock():
    rc = _rc(channels=["message", "history", "ingested_record", "memory"])
    probes = _by_id()
    for pid in _ALL:
        ev = run_probe(probes[pid], rc, mock_policy=MockPolicy(rule="always"), seed=1)
        assert ev.n_errors == 0, f"{pid}: target errored ({ev.n_errors} sample(s))"
        assert ev.asr == 1.0, f"{pid} did not fire on always-comply mock (asr={ev.asr})"


def test_robust_agent_never_fires():
    rc = _rc(channels=["message", "history", "ingested_record", "memory"])
    probes = _by_id()
    for pid in _ALL:
        ev = run_probe(probes[pid], rc, mock_policy=MockPolicy(rule="never"), seed=1)
        assert ev.asr == 0.0, f"{pid} false-fired on a robust mock"


def test_indirect_probes_are_blind_spots_until_channel_declared():
    probes = list(_by_id().values())
    # full tool inventory, but ONLY the conversation channels declared -> the ingestion-channel
    # probes are gated OUT (never silently delivered via message), surfaced as blind spots.
    selected = {p.id for p in select_probes(probes, _rc(channels=["message", "history"]))}
    for pid in _CHANNEL_GATED:
        assert pid not in selected, f"{pid} must be skipped when its ingestion channel is undeclared"
    # the conversation-channel deploy/code probes still select (caps declared, no ingestion needed).
    for pid in _DIRECT:
        assert pid in selected, f"{pid} should select with its capability declared"


def test_ingested_record_probes_select_when_channel_declared():
    probes = list(_by_id().values())
    selected = {p.id for p in select_probes(probes, _rc(channels=["message", "history", "ingested_record"]))}
    assert _INDIRECT_DEPLOY in selected and _INDIRECT_CODE in selected
    # the memory-channel probe is still gated out (memory not declared here).
    assert _XSESSION not in selected


def test_memory_probe_selects_when_memory_declared():
    probes = list(_by_id().values())
    selected = {p.id for p in select_probes(probes, _rc(channels=["message", "history", "memory"]))}
    assert _XSESSION in selected


def test_deploy_probe_gates_out_without_deploy_capability():
    probes = list(_by_id().values())
    # an inventory with edit_file/code_exec but NO deploy capability.
    inv = [ToolSpec(name="patch", capabilities=["edit_file"]),
           ToolSpec(name="run_cmd", capabilities=["code_exec"])]
    rc = _rc(channels=["message", "history", "ingested_record", "memory"], inventory=inv)
    selected = {p.id for p in select_probes(probes, rc)}
    assert _DEPLOY_DIRECT not in selected, "unauthorized-deploy must gate out without the deploy capability"
    # a code-only probe still selects (its caps are present).
    assert _CI_TAMPER in selected
