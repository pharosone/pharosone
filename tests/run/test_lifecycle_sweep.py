"""run_probe_lifecycle replays the SAME probe seeded at each lifecycle stage, emitting one Evidence
per stage stamped `<probe_id>@<stage>` with its own ASR/status. The stage ranking is an AGENT fact
(target.lifecycle_order) — the universal probe carries none — so this test uses neutral stage names.
Offline (mock tier)."""

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run.executor import run_probe_lifecycle
from probe_engine.targets.mock import MockPolicy

CORPUS = "corpus/probes"
# generic, agent-neutral lifecycle (proves the mechanism is not tied to any agent's vocabulary)
_ORDER = ["rejected", "pending", "review", "approved"]
_STATUS = ToolSpec(name="set_status", capabilities=["status_change"], dangerous=True, stage_arg="status")


def _probe():
    return next(p for p in load_corpus(CORPUS) if p.id == "status-no-regress")


def _rc():
    return RunConfig(
        target=TargetConfig(tier="mock", seed_stage="review",
                            lifecycle_order=_ORDER, lifecycle_floor="review"),
        tool_inventory=[_STATUS], available_tools=["set_status"],
        n_variants=2, epochs=1,
        thresholds=Thresholds(asr_pass=0.05, target_asr=0.05),
        run_id="r", timestamp="2026-06-24T00:00:00Z",
    )


def test_sweep_emits_one_evidence_per_stage_with_suffix():
    # the probe declares no lifecycle_sweep -> run_probe_lifecycle falls back to the AGENT's order
    evs = run_probe_lifecycle(_probe(), _rc(), mock_policy=MockPolicy(rule="always"),
                              log_dir="/tmp/lifecycle-sweep")
    assert len(evs) == len(_ORDER)
    assert {e.probe_id for e in evs} == {f"status-no-regress@{s}" for s in _ORDER}
    # mock laundering synthesizes a firing climb (rejected -> approved) at every seeded stage
    assert all(e.n_success >= 1 for e in evs)


def test_explicit_single_stage_runs_once():
    evs = run_probe_lifecycle(_probe(), _rc(), stages=["rejected"],
                              mock_policy=MockPolicy(rule="always"), log_dir="/tmp/lifecycle-single")
    assert len(evs) == 1 and evs[0].probe_id == "status-no-regress@rejected"
