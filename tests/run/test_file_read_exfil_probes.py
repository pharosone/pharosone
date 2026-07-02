"""Regression for the sensitive-file-read-exfil probe family (direct / path-traversal / indirect /
adaptive): they load, target the canonical `read_file` capability + `secret_leaked` oracle, select
correctly (the indirect one only when its ingestion channel is declared), and actually FIRE the
secret_leaked oracle on the offline mock. Fully offline.
"""

from pathlib import Path

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run.executor import run_probe
from probe_engine.run.selection import select_probes
from probe_engine.targets.mock import MockPolicy

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _ROOT / "corpus" / "probes"

_DIRECT = "sensitive-file-read-exfil"
_TRAVERSAL = "sensitive-file-read-path-traversal"
_INDIRECT = "indirect-file-read-exfil"
_ADAPTIVE = "adaptive-file-read-exfil"
_ALL = [_DIRECT, _TRAVERSAL, _INDIRECT, _ADAPTIVE]


def _by_id():
    return {p.id: p for p in load_corpus(str(_CORPUS))}


def _rc(channels=None) -> RunConfig:
    # a tool whose real name (read_file) maps to the canonical read_file capability, and
    # carries leaks_if_path_contains so the secret_leaked oracle is adjudicable on non-mock tiers too.
    inv = [ToolSpec(name="read_file", capabilities=["read_file"],
                    leaks_if_path_contains="id_rsa", dangerous=False)]
    return RunConfig(
        target=TargetConfig(tier="mock", channels=channels or []),
        tool_inventory=inv, n_variants=2, epochs=1,
        thresholds=Thresholds(), run_id="r", timestamp="2026-06-25T00:00:00Z",
    )


def test_all_four_present_and_well_formed():
    probes = _by_id()
    for pid in _ALL:
        assert pid in probes, f"{pid} missing from corpus"
        p = probes[pid]
        assert p.applicability.required_tools == ["read_file"]
        assert p.evaluation.binary.oracle == "secret_leaked"
        assert p.evaluation.semantic is not None        # judge backstop present
        assert p.sandbox and p.sandbox.seed_secret      # a seeded secret for real-tier fidelity


def test_message_channel_probes_select_with_read_file():
    # direct / traversal / adaptive ride the conversation channel -> select on any read_file agent.
    selected = {p.id for p in select_probes(list(_by_id().values()), _rc())}
    assert {_DIRECT, _TRAVERSAL, _ADAPTIVE}.issubset(selected)


def test_indirect_is_blind_spot_until_channel_declared():
    probes = list(_by_id().values())
    # no ingestion channel declared -> the indirect probe is gated OUT (never silently delivered
    # via message); the three conversation-channel probes still select.
    assert _INDIRECT not in {p.id for p in select_probes(probes, _rc())}
    # declare the ingested_record channel -> it now selects.
    assert _INDIRECT in {p.id for p in select_probes(probes, _rc(channels=["ingested_record"]))}


def test_each_fires_secret_leaked_on_mock():
    probes = _by_id()
    rc = _rc(channels=["ingested_record"])  # so the indirect probe is adjudicable here too
    for pid in _ALL:
        ev = run_probe(probes[pid], rc, mock_policy=MockPolicy(rule="always"), seed=1)
        assert ev.n_success == ev.n_trials and ev.asr == 1.0, f"{pid} did not fire on always-comply mock"


def test_robust_agent_never_fires():
    probes = _by_id()
    rc = _rc(channels=["ingested_record"])
    for pid in _ALL:
        ev = run_probe(probes[pid], rc, mock_policy=MockPolicy(rule="never"), seed=1)
        assert ev.n_success == 0 and ev.asr == 0.0, f"{pid} false-fired on a robust mock"
